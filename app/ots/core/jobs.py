"""PostgreSQL-backed job scheduler (no broker). Jobs must be safe to re-run.

Locking: one session advisory lock per job name, plus the shared 'ots_heavy' lock for heavy jobs, so two
workers never run the same job and heavy jobs never overlap. Each run executes in its own thread (own DB
connection) so a long upload cannot starve the 60 s attendance sweep.
"""

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from croniter import croniter
from django.db import connection

from .clock import IST, now
from .models import Job, JobRun

log = logging.getLogger(__name__)

CONTINUOUS = "continuous"
HEAVY_LOCK = "ots_heavy"
OVERDUE_AFTER = timedelta(minutes=30)
MAX_THREADS = 4

# M00.8 wires these to alerts.raise_alert / alerts.resolve_alert.
alert_hook: Callable[[str, str, str, str], None] | None = None
resolve_hook: Callable[[str], None] | None = None


@dataclass(frozen=True)
class JobDef:
    name: str
    schedule: str
    heavy: bool
    fn: Callable[[], Any]


_JOBS: dict[str, JobDef] = {}


def job(name: str, schedule: str, heavy: bool = False) -> Callable[[Callable[[], Any]], Callable[[], Any]]:
    if schedule != CONTINUOUS and not croniter.is_valid(schedule):
        raise ValueError(f"invalid cron schedule for {name}: {schedule}")

    def register(fn: Callable[[], Any]) -> Callable[[], Any]:
        _JOBS[name] = JobDef(name, schedule, heavy, fn)
        return fn

    return register


def registered_jobs() -> dict[str, JobDef]:
    return dict(_JOBS)


def next_run_after(schedule: str, after: datetime) -> datetime | None:
    """Next fire time (aware) after `after`, evaluated in Asia/Kolkata. None for continuous jobs."""
    if schedule == CONTINUOUS:
        return None
    nxt: datetime = croniter(schedule, after.astimezone(IST)).get_next(datetime)
    return nxt


def _try_lock(name: str) -> bool:
    with connection.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(hashtext(%s))", [name])
        row = cur.fetchone()
        return bool(row and row[0])


def _unlock(name: str) -> None:
    with connection.cursor() as cur:
        cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", [name])


class Scheduler:
    def __init__(self, max_threads: int = MAX_THREADS) -> None:
        self.max_threads = max_threads
        self._running: set[str] = set()
        self._threads: list[threading.Thread] = []
        self._guard = threading.Lock()

    # -- start-up ---------------------------------------------------------
    def sync_registry(self) -> None:
        t = now()
        for d in _JOBS.values():
            row, created = Job.objects.get_or_create(
                name=d.name, defaults={"schedule": d.schedule, "next_run_at": next_run_after(d.schedule, t)}
            )
            if not created and row.schedule != d.schedule:  # keep `enabled`, refresh the schedule
                row.schedule = d.schedule
                row.next_run_at = next_run_after(d.schedule, t)
                row.save(update_fields=["schedule", "next_run_at"])

    def startup_sweep(self) -> int:
        """Close RUNNING runs left by a dead worker as ABORTED (F-CORE-2). A run whose job lock is still
        held belongs to a live worker and is left alone."""
        closed = 0
        for name in (
            JobRun.objects.filter(status=JobRun.Status.RUNNING).values_list("job_name", flat=True).distinct()
        ):
            if not _try_lock(name):
                continue
            try:
                n = JobRun.objects.filter(job_name=name, status=JobRun.Status.RUNNING).update(
                    status=JobRun.Status.ABORTED, finished_at=now(), detail={"reason": "worker died mid-run"}
                )
                Job.objects.filter(name=name).update(last_status="ABORTED", last_finished_at=now())
                closed += n
            finally:
                _unlock(name)
        return closed

    # -- running ----------------------------------------------------------
    def due(self, at: datetime) -> list[JobDef]:
        out = []
        rows = {j.name: j for j in Job.objects.filter(enabled=True)}
        for d in _JOBS.values():
            row = rows.get(d.name)
            if row is None:
                continue
            if d.schedule == CONTINUOUS or (row.next_run_at is not None and row.next_run_at <= at):
                out.append(d)
        return out

    def run_job(self, d: JobDef) -> str:
        """Run one job synchronously in the calling thread. Returns SUCCESS, FAILED or SKIPPED."""
        if not _try_lock(d.name):
            return "SKIPPED"
        heavy = False
        try:
            if d.heavy:
                heavy = _try_lock(HEAVY_LOCK)
                if not heavy:
                    return "SKIPPED"
            return self._execute(d)
        finally:
            if heavy:
                _unlock(HEAVY_LOCK)
            _unlock(d.name)

    def _execute(self, d: JobDef) -> str:
        started = now()
        continuous = d.schedule == CONTINUOUS
        run = None if continuous else JobRun.objects.create(job_name=d.name, started_at=started)
        Job.objects.filter(name=d.name).update(last_started_at=started)
        status, error = "SUCCESS", ""
        try:
            d.fn()
        except Exception as exc:
            log.exception("job failed", extra={"event": f"job.failed.{d.name}"})
            status, error = "FAILED", f"{type(exc).__name__}: {exc}"[:2000]
            if not connection.in_atomic_block:
                connection.close_if_unusable_or_obsolete()
        finished = now()
        if run is not None:
            JobRun.objects.filter(pk=run.pk).update(
                status=status, finished_at=finished, detail={"error": error} if error else {}
            )
        elif status == "FAILED":
            JobRun.objects.create(
                job_name=d.name,
                started_at=started,
                finished_at=finished,
                status=status,
                detail={"error": error},
            )
        Job.objects.filter(name=d.name).update(
            last_finished_at=finished,
            last_status=status,
            last_error=error,
            next_run_at=next_run_after(d.schedule, finished),
        )
        return status

    def tick(self) -> list[threading.Thread]:
        """Start a thread for every due job that is not already running here."""
        started = []
        for d in self.due(now()):
            with self._guard:
                if d.name in self._running or len(self._running) >= self.max_threads:
                    continue
                self._running.add(d.name)
            t = threading.Thread(target=self._thread_main, args=(d,), name=f"job:{d.name}", daemon=True)
            self._threads.append(t)
            t.start()
            started.append(t)
        self._threads = [t for t in self._threads if t.is_alive()]
        return started

    def _thread_main(self, d: JobDef) -> None:
        try:
            self.run_job(d)
        except Exception:
            log.exception("scheduler thread crashed", extra={"event": "job.thread_crashed"})
        finally:
            connection.close()
            with self._guard:
                self._running.discard(d.name)

    def wait(self, timeout: float = 30.0) -> bool:
        """Wait for running jobs (graceful shutdown). True if all finished."""
        for t in list(self._threads):
            t.join(timeout=timeout)
        return not any(t.is_alive() for t in self._threads)


def watchdog() -> None:
    """Alert when a scheduled job is >30 min overdue or failed twice in a row."""
    t = now()
    for row in Job.objects.filter(enabled=True).exclude(schedule=CONTINUOUS):
        key = f"job.overdue.{row.name}"
        overdue = row.next_run_at is not None and t - row.next_run_at > OVERDUE_AFTER
        last_two = list(
            JobRun.objects.filter(job_name=row.name).exclude(status=JobRun.Status.RUNNING).order_by("-id")[:2]
        )
        failing = len(last_two) == 2 and all(r.status == JobRun.Status.FAILED for r in last_two)
        if overdue or failing:
            why = "overdue" if overdue else "failed twice in a row"
            if alert_hook:
                alert_hook(key, "WARNING", f"Job {row.name} {why}", row.last_error or why)
        elif resolve_hook:
            resolve_hook(key)
