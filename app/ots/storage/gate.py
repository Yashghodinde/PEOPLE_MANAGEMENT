"""The queue gate: may the next storage operation run, and what do we do when one fails? (M05 section 3)

State lives in the `alerts` table, so a worker restart does not forget a block:
* `storage.ops_block`: the queue-wide block (BLOCKED_AUTH or BLOCKED_QUOTA), with when to probe next;
* `storage.throttled`: Microsoft told us to wait until a given time.
Only "no Graph call has succeeded for 10 minutes" is kept in memory: it restarts counting after a restart.

While blocked the queue runs ONE probe operation every 15 minutes; a success clears the block by itself.
"""

from __future__ import annotations

import datetime
import json
import random
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ots.core import alerts
from ots.core.clock import now as server_now
from ots.core.models import Alert

from . import classify as c

BLOCK_KEY = "storage.ops_block"
BLOCKED_AUTH = "BLOCKED_AUTH"
BLOCKED_QUOTA = "BLOCKED_QUOTA"
PROBE_EVERY = datetime.timedelta(minutes=15)
OFFLINE_AFTER = datetime.timedelta(minutes=10)
SCOPE_ALL = "ALL"
SCOPE_ARCHIVE = "ARCHIVE_ONLY"  # capacity >= 95%: new files wait, recovery data (WAL, backups) still flows
ARCHIVE_KIND = "ARCHIVE_FILE"


class Permission(StrEnum):
    RUN = "RUN"
    PROBE = "PROBE"  # blocked, but it is time to try one operation
    WAIT = "WAIT"


@dataclass(frozen=True)
class Decision:
    permission: Permission
    wake_at: datetime.datetime | None = None  # when to ask again (WAIT)
    reason: str = ""


@dataclass(frozen=True)
class Block:
    state: str
    scope: str
    since: datetime.datetime
    next_probe_at: datetime.datetime
    reason: str


def _load(alert: Alert | None) -> dict[str, Any]:
    if alert is None:
        return {}
    try:
        data = json.loads(alert.detail or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _when(text: Any, fallback: datetime.datetime) -> datetime.datetime:
    try:
        return datetime.datetime.fromisoformat(str(text))
    except ValueError:
        return fallback


class QueueGate:
    def __init__(
        self,
        clock: Callable[[], datetime.datetime] = server_now,
        rng: Callable[[], float] = lambda: random.uniform(0.8, 1.2),  # noqa: S311  (backoff jitter, not security)
    ) -> None:
        self._now = clock
        self._rng = rng
        self._failing_since: datetime.datetime | None = None

    # ---- reading state ---------------------------------------------------------------------------
    def block(self) -> Block | None:
        alert = alerts.open_alerts().filter(key=BLOCK_KEY).first()
        if alert is None:
            return None
        d = _load(alert)
        t = self._now()
        return Block(
            state=alert.title,
            scope=d.get("scope", SCOPE_ALL),
            since=_when(d.get("since"), alert.first_seen_at),
            next_probe_at=_when(d.get("next_probe_at"), t),
            reason=d.get("reason", ""),
        )

    def paused_until(self) -> datetime.datetime | None:
        alert = alerts.open_alerts().filter(key=c.ALERT_THROTTLED).first()
        until = _load(alert).get("until")
        return _when(until, self._now()) if until else None

    # ---- the question the queue asks before every operation -----------------------------------------
    def permission(self, kind: str = "") -> Decision:
        t = self._now()
        until = self.paused_until()
        if until is not None:
            if until > t:
                return Decision(Permission.WAIT, until, "throttled by Microsoft")
            alerts.resolve_alert(c.ALERT_THROTTLED)
        block = self.block()
        if block is None:
            return Decision(Permission.RUN)
        if block.scope == SCOPE_ARCHIVE:
            if kind == ARCHIVE_KIND:
                return Decision(Permission.WAIT, None, "storage is almost full: new files wait")
            return Decision(Permission.RUN)  # WAL and backups continue into the remaining space
        if block.next_probe_at <= t:
            return Decision(Permission.PROBE, None, f"{block.state}: probing")
        return Decision(Permission.WAIT, block.next_probe_at, block.state)

    # ---- reporting what happened -------------------------------------------------------------------
    def success(self, *, probe: bool = False) -> None:
        """An operation (or a probe) got a good answer from Microsoft."""
        self._failing_since = None
        alerts.resolve_alert(c.ALERT_OFFLINE)
        block = self.block()
        if block is not None and block.scope == SCOPE_ALL and probe:
            self.clear_block()

    def failure(
        self, error: BaseException, *, attempts: int = 0, first_attempt_at: datetime.datetime | None = None,
        probe: bool = False,
    ) -> c.Action:  # fmt: skip
        """Classify, remember what the queue must know (block, pause, offline), and return the action."""
        t = self._now()
        age = t - first_attempt_at if first_attempt_at is not None else datetime.timedelta(0)
        action = c.classify(error, attempts=attempts, age=age, jitter=self._rng())
        if action.kind in (c.Kind.BLOCK_AUTH, c.Kind.BLOCK_QUOTA):
            self._set_block(
                BLOCKED_AUTH if action.kind == c.Kind.BLOCK_AUTH else BLOCKED_QUOTA, SCOPE_ALL, action.message
            )
            self._raise(action)
        elif action.kind == c.Kind.PAUSE_QUEUE:
            delay = action.delay or c.DEFAULT_THROTTLE_SECONDS
            until = t + datetime.timedelta(seconds=delay)
            alerts.raise_alert(
                c.ALERT_THROTTLED, "INFO", "Microsoft asked us to slow down",
                json.dumps({"until": until.isoformat(), "reason": action.message}),
            )  # fmt: skip
        elif action.kind == c.Kind.RETRY:
            self._note_failing(t)
        elif action.kind == c.Kind.FAILED:
            self._raise(action)
        if probe and action.kind not in (c.Kind.BLOCK_AUTH, c.Kind.BLOCK_QUOTA):
            # the probe failed for another reason: keep the block, try again in 15 minutes
            self._schedule_probe()
        return action

    # ---- the offline warning (no call has succeeded for 10 minutes) ---------------------------------
    def _note_failing(self, t: datetime.datetime) -> None:
        if self._failing_since is None:
            self._failing_since = t
        if t - self._failing_since >= OFFLINE_AFTER:
            alerts.raise_alert(
                c.ALERT_OFFLINE, "WARNING", "Microsoft 365 cannot be reached",
                f"no successful call since {self._failing_since.isoformat()}",
            )  # fmt: skip

    # ---- blocks --------------------------------------------------------------------------------------
    def _raise(self, action: c.Action) -> None:
        if action.alert_key and action.severity:
            alerts.raise_alert(
                action.alert_key, action.severity, f"storage: {action.kind.value}", action.message[:2048]
            )

    def _set_block(self, state: str, scope: str, reason: str) -> None:
        t = self._now()
        existing = self.block()
        since = existing.since if existing is not None and existing.state == state else t
        alerts.raise_alert(
            BLOCK_KEY, "CRITICAL", state,
            json.dumps({
                "scope": scope, "since": since.isoformat(), "reason": reason[:500],
                "next_probe_at": (t + PROBE_EVERY).isoformat(),
            }),
        )  # fmt: skip

    def _schedule_probe(self) -> None:
        block = self.block()
        if block is not None:
            self._set_block(block.state, block.scope, block.reason)  # keeps `since`, moves next_probe_at

    def block_archive_only(self, reason: str) -> None:
        """Capacity >= 95%: only ARCHIVE_FILE operations wait (M05.7)."""
        self._set_block(BLOCKED_QUOTA, SCOPE_ARCHIVE, reason)

    def clear_archive_block(self) -> None:
        """Space is back below the limit: lift ONLY the archive-only block (never an auth or quota block)."""
        block = self.block()
        if block is not None and block.scope == SCOPE_ARCHIVE:
            alerts.resolve_alert(BLOCK_KEY)

    def clear_block(self) -> None:
        alerts.resolve_alert(BLOCK_KEY)
        alerts.resolve_alert(c.ALERT_AUTH)
        alerts.resolve_alert(c.ALERT_QUOTA)

    def retry_now(self) -> None:
        """The Super Admin's 'Retry now': the next operation is a probe immediately."""
        block = self.block()
        if block is None:
            return
        alert = alerts.open_alerts().filter(key=BLOCK_KEY).first()
        if alert is not None:
            d = _load(alert)
            d["next_probe_at"] = self._now().isoformat()
            alerts.raise_alert(BLOCK_KEY, "CRITICAL", block.state, json.dumps(d))


gate = QueueGate()
