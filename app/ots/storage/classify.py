"""One pure function that decides what to do about a storage error (M05 section 3).

`classify(error, attempts=..., age=...)` knows nothing about the database or the clock: it takes the error
that a provider raised and returns an `Action`. The gate (gate.py) applies the action; the queue (M05.4)
obeys the gate.
That keeps the whole error table in one place and testable row by row.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from enum import StrEnum

from .errors import ConfigurationError, GraphError, TransportFailure, VerificationFailed, WrongLibrary

# Alert keys (one stable key per condition; raising again bumps a counter)
ALERT_AUTH = "storage.auth"
ALERT_QUOTA = "storage.quota"
ALERT_OFFLINE = "storage.offline"
ALERT_THROTTLED = "storage.throttled"
ALERT_CONFLICT = "storage.conflict"
ALERT_CERT = "storage.cert_expiring"
ALERT_DEAD_LETTER = "storage.dead_letter"
ALERT_VERIFICATION = "storage.verification"
ALERT_CAPACITY = "storage.capacity"  # SharePoint is filling up
ALERT_ORPHAN = "storage.orphan"  # an unknown file in Business-Files was moved to quarantine
ALERT_MISSING = "storage.missing"  # a file we archived is gone from SharePoint
ALERT_DRIFTED = "storage.drifted"  # renamed or moved in SharePoint (identity is the item id, so harmless)
ALERT_CONTENT_CHANGED = "storage.content_changed"  # the bytes in SharePoint differ from what we uploaded

BACKOFF_BASE = 30.0  # seconds
BACKOFF_CAP = 3600.0
GIVE_UP_AFTER = datetime.timedelta(hours=24)
VERIFICATION_RETRIES = 3
DEFAULT_THROTTLE_SECONDS = 60.0  # a 429 that does not say how long to wait


class Kind(StrEnum):
    BLOCK_AUTH = "BLOCK_AUTH"  # whole queue stops; probe every 15 min
    BLOCK_QUOTA = "BLOCK_QUOTA"  # whole queue stops; probe every 15 min
    PAUSE_QUEUE = "PAUSE_QUEUE"  # throttled: the whole queue waits `delay` seconds
    RETRY = "RETRY"  # this operation waits `delay` seconds, then tries again
    SESSION_EXPIRED = "SESSION_EXPIRED"  # uploadUrl vanished: look for the item, adopt or start over
    CONFLICT = "CONFLICT"  # name exists: compare size and hash, adopt or dead-letter
    RESUME = "RESUME"  # 416: ask the server where to continue
    RESYNC = "RESYNC"  # 410: the delta cursor is gone, do a full inventory
    FAILED = "FAILED"  # dead letter: stop trying, alert, keep the local file


@dataclass(frozen=True)
class Action:
    kind: Kind
    delay: float | None = None  # seconds (PAUSE_QUEUE, RETRY)
    alert_key: str | None = None
    severity: str | None = None  # "INFO" | "WARNING" | "CRITICAL"
    message: str = ""


def backoff_seconds(attempts: int, jitter: float = 1.0) -> float:
    """30 s x 2^attempts, capped at 60 min; `jitter` is a multiplier around 1.0 (the gate passes 0.8..1.2)."""
    return float(min(BACKOFF_BASE * float(2 ** max(0, attempts)), BACKOFF_CAP) * jitter)


def _retry(attempts: int, age: datetime.timedelta, jitter: float, what: str) -> Action:
    if age >= GIVE_UP_AFTER:
        return Action(
            Kind.FAILED, alert_key=ALERT_DEAD_LETTER, severity="CRITICAL",
            message=f"gave up after {age} of attempts ({what})",
        )  # fmt: skip
    return Action(Kind.RETRY, delay=backoff_seconds(attempts, jitter), message=what)


def classify(
    error: BaseException,
    *,
    attempts: int = 0,
    age: datetime.timedelta = datetime.timedelta(0),
    jitter: float = 1.0,
) -> Action:
    """What to do about `error`. `attempts` = tries so far; `age` = time since the first try."""
    if isinstance(error, GraphError):
        s = error.status
        what = f"graph {s} {error.code}".strip()
        if s in (401, 403):  # a 401 reaching here already survived the provider's one token refresh
            return Action(
                Kind.BLOCK_AUTH, alert_key=ALERT_AUTH, severity="CRITICAL", message=f"{what}: {error.body}"
            )
        if s == 404 and error.url_kind == "upload":
            return Action(Kind.SESSION_EXPIRED, message=what)
        if s == 409:
            return Action(Kind.CONFLICT, message=what)
        if s == 410:
            return Action(Kind.RESYNC, message=what)
        if s == 416:
            return Action(Kind.RESUME, message=what)
        if s == 429 or (s == 503 and error.retry_after is not None):
            return Action(
                Kind.PAUSE_QUEUE,
                delay=error.retry_after if error.retry_after is not None else DEFAULT_THROTTLE_SECONDS,
                alert_key=ALERT_THROTTLED, severity="INFO", message=what,
            )  # fmt: skip
        if s == 507:
            return Action(
                Kind.BLOCK_QUOTA, alert_key=ALERT_QUOTA, severity="CRITICAL", message=f"{what}: {error.body}"
            )
        if s in (500, 502, 503, 504):
            return _retry(attempts, age, jitter, what)
        if 400 <= s < 500:
            return Action(
                Kind.FAILED,
                alert_key=ALERT_DEAD_LETTER,
                severity="CRITICAL",
                message=f"{what}: {error.body[:2048]}",
            )
        return _retry(
            attempts, age, jitter, what
        )  # anything else unexpected (3xx, 1xx, other 5xx): try again
    if isinstance(error, TransportFailure):
        return _retry(attempts, age, jitter, f"no answer: {error}")
    if isinstance(error, VerificationFailed):
        if attempts + 1 >= VERIFICATION_RETRIES:
            return Action(
                Kind.FAILED,
                alert_key=ALERT_VERIFICATION,
                severity="CRITICAL",
                message=f"verification failed: {error}",
            )
        return _retry(attempts, age, jitter, f"verification failed: {error}")
    if isinstance(error, ConfigurationError):  # no certificate, no settings: we cannot authenticate
        return Action(Kind.BLOCK_AUTH, alert_key=ALERT_AUTH, severity="CRITICAL", message=str(error))
    if isinstance(error, WrongLibrary):  # a programming error, never retried
        return Action(Kind.FAILED, alert_key=ALERT_DEAD_LETTER, severity="CRITICAL", message=str(error))
    return _retry(attempts, age, jitter, f"unexpected {type(error).__name__}: {error}")
