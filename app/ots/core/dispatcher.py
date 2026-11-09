"""Outbox dispatcher: delivers unpublished events to subscribers, marks them published afterwards."""

import logging
import time
from collections.abc import Callable
from typing import Any

import psycopg
from django.conf import settings
from django.db import connection, transaction

from .events import NOTIFY_CHANNEL, Event, handlers_for
from .models import OutboxEvent

log = logging.getLogger(__name__)

BATCH = 100
MAX_ATTEMPTS = 10
AlertHook = Callable[..., object]  # (key, severity, title, detail)
# M00.8 sets this to alerts.raise_alert so failures surface to the Super Admin.
alert_hook: AlertHook | None = None


def _alert(key: str, severity: str, title: str, detail: str) -> None:
    if alert_hook is not None:
        alert_hook(key, severity, title, detail)


class Dispatcher:
    """Retry state is in memory: attempts back off 2**n seconds (cap 300 s); after MAX_ATTEMPTS the event is
    dead-lettered in its payload and a CRITICAL alert is raised."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._state: dict[int, tuple[int, float]] = {}  # id -> (attempts, retry_at)

    def run_once(self) -> int:
        """Deliver one batch. Returns the number of events marked published (or dead-lettered)."""
        done = 0
        with transaction.atomic():
            rows = list(
                OutboxEvent.objects.select_for_update(skip_locked=True)
                .filter(published_at__isnull=True)
                .order_by("id")[:BATCH]
            )
            for row in rows:
                attempts, retry_at = self._state.get(row.id, (0, 0.0))
                if self._clock() < retry_at:
                    continue
                event = Event(row.id, row.type, row.aggregate_type, row.aggregate_id, row.payload, row.at)
                error = self._deliver(event)
                if error is None:
                    self._mark("SELECT ots_mark_published(%s)", [row.id])
                    self._state.pop(row.id, None)
                    done += 1
                    continue
                attempts += 1
                if attempts >= MAX_ATTEMPTS:
                    self._mark("SELECT ots_mark_dead_letter(%s, %s)", [row.id, error[:500]])
                    self._state.pop(row.id, None)
                    _alert("outbox.dead_letter", "CRITICAL", f"Event {row.id} dead-lettered", error)
                    done += 1
                else:
                    self._state[row.id] = (attempts, self._clock() + min(2**attempts, 300))
                    _alert("outbox.handler_failed", "WARNING", f"Handler failed for event {row.id}", error)
        return done

    def _deliver(self, event: Event) -> str | None:
        for handler in handlers_for(event.type):
            try:
                handler(event)
            except Exception as exc:
                log.exception("outbox handler failed", extra={"event": "outbox.handler_failed"})
                return f"{type(exc).__name__}: {exc}"
        return None

    @staticmethod
    def _mark(sql: str, params: list[Any]) -> None:
        with connection.cursor() as cur:
            cur.execute(sql, params)


class Listener:
    """Dedicated autocommit connection that LISTENs on the outbox channel."""

    def __init__(self) -> None:
        db = settings.DATABASES["default"]
        self._conn = psycopg.connect(
            dbname=db["NAME"],
            user=db.get("USER") or None,
            password=db.get("PASSWORD") or None,
            host=db.get("HOST") or None,
            port=db.get("PORT") or None,
            autocommit=True,
        )
        self._conn.execute(f"LISTEN {NOTIFY_CHANNEL}")

    def wait(self, timeout: float = 5.0) -> bool:
        """True if a notification arrived within timeout seconds."""
        for _ in self._conn.notifies(timeout=timeout, stop_after=1):
            return True
        return False

    def close(self) -> None:
        self._conn.close()
