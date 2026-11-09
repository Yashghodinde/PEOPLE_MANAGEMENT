import datetime as dt
import threading
from zoneinfo import ZoneInfo

import pytest
from django.db import connection, connections
from freezegun import freeze_time
from ots.core import jobs
from ots.core.jobs import JobDef, Scheduler, next_run_after
from ots.core.models import Job, JobRun

IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture(autouse=True)
def clean_registry():
    saved = dict(jobs._JOBS)
    jobs._JOBS.clear()
    jobs.alert_hook = jobs.resolve_hook = None
    yield
    jobs._JOBS.clear()
    jobs._JOBS.update(saved)
    jobs.alert_hook = jobs.resolve_hook = None


def test_schedule_evaluated_in_ist_across_midnight():
    # 2026-11-09 18:00 UTC == 23:30 IST. "45 22 * * *" fires 22:45 IST next day = 17:15 UTC on the 10th.
    after = dt.datetime(2026, 11, 9, 18, 0, tzinfo=dt.UTC)
    nxt = next_run_after("45 22 * * *", after)
    assert nxt == dt.datetime(2026, 11, 10, 22, 45, tzinfo=IST)
    assert nxt.astimezone(dt.UTC) == dt.datetime(2026, 11, 10, 17, 15, tzinfo=dt.UTC)
    # just before midnight IST -> next 00:00 job is minutes away
    assert next_run_after("0 0 * * *", dt.datetime(2026, 11, 9, 18, 29, tzinfo=dt.UTC)) == dt.datetime(
        2026, 11, 10, 0, 0, tzinfo=IST
    )
    assert next_run_after(jobs.CONTINUOUS, after) is None


def test_invalid_cron_rejected():
    with pytest.raises(ValueError):
        jobs.job("bad", schedule="not a cron")


@pytest.mark.django_db
def test_sync_registry_inserts_and_keeps_enabled_flag():
    jobs.job("a.job", schedule="0 3 * * *")(lambda: None)
    s = Scheduler()
    s.sync_registry()
    Job.objects.filter(name="a.job").update(enabled=False)
    jobs._JOBS["a.job"] = JobDef("a.job", "0 4 * * *", False, lambda: None)
    s.sync_registry()
    row = Job.objects.get(name="a.job")
    assert row.enabled is False and row.schedule == "0 4 * * *"


@pytest.mark.django_db
def test_new_cron_job_does_not_run_immediately_but_missed_one_runs_once():
    calls = []
    jobs.job("c.job", schedule="0 3 * * *")(lambda: calls.append(1))
    s = Scheduler()
    s.sync_registry()
    assert s.due(jobs.now()) == []  # brand-new job waits for its slot
    Job.objects.filter(name="c.job").update(next_run_at=jobs.now() - dt.timedelta(hours=9))  # server was off
    assert [d.name for d in s.due(jobs.now())] == ["c.job"]
    assert s.run_job(jobs._JOBS["c.job"]) == "SUCCESS" and calls == [1]
    assert s.due(jobs.now()) == []  # exactly one catch-up run, no pile-up
    assert Job.objects.get(name="c.job").next_run_at > jobs.now()


@pytest.mark.django_db
def test_failure_recorded_with_error():
    def boom():
        raise ValueError("kaput")

    jobs.job("f.job", schedule="0 3 * * *")(boom)
    s = Scheduler()
    s.sync_registry()
    assert s.run_job(jobs._JOBS["f.job"]) == "FAILED"
    run = JobRun.objects.get(job_name="f.job")
    assert run.status == "FAILED" and "kaput" in run.detail["error"]
    assert "kaput" in Job.objects.get(name="f.job").last_error


@pytest.mark.django_db
def test_continuous_job_records_run_row_only_on_failure():
    jobs.job("k.job", schedule=jobs.CONTINUOUS)(lambda: None)
    s = Scheduler()
    s.sync_registry()
    s.run_job(jobs._JOBS["k.job"])
    assert JobRun.objects.filter(job_name="k.job").count() == 0
    assert Job.objects.get(name="k.job").last_status == "SUCCESS"


@pytest.mark.django_db
def test_watchdog_flags_overdue_and_double_failure_and_resolves():
    alerts, resolved = [], []
    jobs.alert_hook = lambda *a: alerts.append(a[0])
    jobs.resolve_hook = lambda k: resolved.append(k)
    for name in ("late.job", "flaky.job", "fine.job"):
        jobs.job(name, schedule="0 3 * * *")(lambda: None)
    Scheduler().sync_registry()
    Job.objects.filter(name="late.job").update(next_run_at=jobs.now() - dt.timedelta(minutes=31))
    Job.objects.filter(name="fine.job").update(next_run_at=jobs.now() - dt.timedelta(minutes=29))
    for _ in range(2):
        JobRun.objects.create(job_name="flaky.job", started_at=jobs.now(), status="FAILED")
    jobs.watchdog()
    assert sorted(alerts) == ["job.overdue.flaky.job", "job.overdue.late.job"]
    assert resolved == ["job.overdue.fine.job"]


@pytest.mark.django_db(transaction=True)
def test_two_connections_never_run_same_job_concurrently():
    gate, entered = threading.Event(), threading.Event()
    results = {}

    def slow():
        entered.set()
        gate.wait(10)

    jobs.job("lock.job", schedule="0 3 * * *")(slow)
    Scheduler().sync_registry()
    d = jobs._JOBS["lock.job"]

    def first():
        try:
            results["first"] = Scheduler().run_job(d)
        finally:
            connections.close_all()

    t = threading.Thread(target=first)
    t.start()
    assert entered.wait(10)
    assert Scheduler().run_job(d) == "SKIPPED"  # second connection cannot take the lock
    gate.set()
    t.join(10)
    assert results["first"] == "SUCCESS"
    assert Scheduler().run_job(d) == "SUCCESS"  # lock released afterwards


@pytest.mark.django_db(transaction=True)
def test_heavy_mutex_serialises_heavy_jobs():
    gate, entered = threading.Event(), threading.Event()
    out = {}

    def heavy_a():
        entered.set()
        gate.wait(10)

    jobs.job("heavy.a", schedule="0 3 * * *", heavy=True)(heavy_a)
    jobs.job("heavy.b", schedule="0 3 * * *", heavy=True)(lambda: None)
    Scheduler().sync_registry()

    def run_a():
        try:
            out["a"] = Scheduler().run_job(jobs._JOBS["heavy.a"])
        finally:
            connections.close_all()

    t = threading.Thread(target=run_a)
    t.start()
    assert entered.wait(10)
    assert Scheduler().run_job(jobs._JOBS["heavy.b"]) == "SKIPPED"  # different job, same heavy mutex
    gate.set()
    t.join(10)
    assert Scheduler().run_job(jobs._JOBS["heavy.b"]) == "SUCCESS"


@pytest.mark.django_db(transaction=True)
def test_tick_runs_due_jobs_in_threads_without_blocking():
    gate = threading.Event()
    ran = []
    jobs.job("slow.job", schedule=jobs.CONTINUOUS)(lambda: gate.wait(10))
    jobs.job("fast.job", schedule=jobs.CONTINUOUS)(lambda: ran.append(1))
    s = Scheduler()
    s.sync_registry()
    started = s.tick()
    assert len(started) == 2
    for t in started:
        if t.name == "job:fast.job":
            t.join(10)
    assert ran == [1], "a slow job must not block a fast one"
    gate.set()
    assert s.wait(10)
    assert connection.is_usable()


@freeze_time("2026-11-09")
def test_freeze_time_does_not_break_next_run():
    assert next_run_after("* * * * *", dt.datetime(2026, 11, 9, tzinfo=dt.UTC)) is not None
