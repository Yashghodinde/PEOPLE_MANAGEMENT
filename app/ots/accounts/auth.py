"""Password login: Argon2id check, lockout (5 failures / 15 min -> 15 min lock), audit of every attempt.

Audit rows for failures must survive the failure, so the attempt is decided inside one transaction and
the error is raised only after it has committed.

How failures are counted: wrong passwords on /login and wrong current passwords on /password/change both
write an audit row. The count is the number of those rows inside the last 15 minutes AND after the most
recent "reset point" for that user (a good login, a password change or an admin reset), so a reset really
starts a clean count. Unknown or inactive login IDs are counted by the ID text in the same way, so a
real ID and a made-up one behave identically (same status code, same timing).
"""

from __future__ import annotations

import datetime
import math
from dataclasses import dataclass
from typing import Any

from django.contrib.auth.hashers import check_password, make_password
from django.db import transaction

from ots.audit import api as audit
from ots.audit.models import AuditLog
from ots.core import alerts
from ots.core.clock import now
from ots.core.errors import Locked, ValidationFailed

from .models import User

MAX_FAILURES = 5
WINDOW = datetime.timedelta(minutes=15)
LOCK_FOR = datetime.timedelta(minutes=15)
GENERIC_FAIL = "Wrong login ID or password."
FAILURE_ACTIONS = ("auth.login_failed", "auth.password_change_failed")
RESET_ACTIONS = ("auth.login_ok", "user.password_changed", "user.password_reset", "breakglass.reset_master")
_DUMMY_HASH: str | None = None


@dataclass
class _Outcome:
    user: User | None = None
    locked_minutes: int = 0
    failed: bool = False
    breakglass_failed: str = ""


def _burn_time(password: str) -> None:
    """Spend one password-hash worth of time, so response time does not reveal which branch ran."""
    global _DUMMY_HASH  # noqa: PLW0603
    if _DUMMY_HASH is None:
        _DUMMY_HASH = make_password("not-a-real-password")
    check_password(password, _DUMMY_HASH)


def minutes_left(until: datetime.datetime, t: datetime.datetime) -> int:
    return max(1, math.ceil((until - t).total_seconds() / 60))


def _key(login_id: str) -> str:
    return login_id.strip().lower()[:60]


def _count_failures(user: User, t: datetime.datetime) -> int:
    """Failures for this user in the last WINDOW, ignoring everything before the latest reset point."""
    target = {"target_type": "user", "target_id": str(user.pk)}
    last_reset = (
        AuditLog.objects.filter(action__in=RESET_ACTIONS, **target)
        .order_by("-at")
        .values_list("at", flat=True)
        .first()
    )
    start = t - WINDOW
    if last_reset is not None and last_reset > start:
        start = last_reset
    return AuditLog.objects.filter(action__in=FAILURE_ACTIONS, at__gt=start, **target).count()


def record_failure(user: User, request: Any, action: str, reason: str, t: datetime.datetime) -> int:
    """Write the failure row, update the counter, lock at the limit. Returns minutes locked (0 = not locked).

    Must run inside a transaction. The break-glass account is never locked (that would let anyone who
    knows its login take it out of service); a failure on it raises a CRITICAL alert instead.
    """
    audit.record(action, user, user, {"reason": reason}, request)
    count = _count_failures(user, t)
    user.failed_logins = count
    if count >= MAX_FAILURES and not user.is_break_glass:
        user.locked_until = t + LOCK_FOR
        audit.record("auth.locked", user, user, {"until": user.locked_until.isoformat()}, request)
        user.save(update_fields=["failed_logins", "locked_until"])
        return minutes_left(user.locked_until, t)
    user.save(update_fields=["failed_logins"])
    return 0


def _unknown_lock_minutes(key: str, t: datetime.datetime) -> int:
    """Lock state for a login ID that matches no active user, kept only in audit rows."""
    rows = list(
        AuditLog.objects.filter(
            action="auth.login_failed", payload__login_id=key, payload__reason="unknown", at__gt=t - WINDOW
        )
        .order_by("-at")
        .values_list("at", flat=True)[:MAX_FAILURES]
    )
    if len(rows) < MAX_FAILURES:
        return 0
    until = rows[0] + LOCK_FOR
    return minutes_left(until, t) if until > t else 0


def _attempt(login_id: str, password: str, request: Any) -> _Outcome:
    t = now()
    key = _key(login_id)
    with transaction.atomic():
        user = User.objects.select_for_update().filter(login_id__iexact=login_id.strip()).first()
        if user is None or not user.is_active:
            _burn_time(password)
            minutes = _unknown_lock_minutes(key, t)
            if minutes:
                audit.record("auth.login_blocked", None, None, {"login_id": key, "reason": "locked"}, request)
                return _Outcome(locked_minutes=minutes)
            audit.record("auth.login_failed", None, None, {"login_id": key, "reason": "unknown"}, request)
            return _Outcome(failed=True)
        password_ok = user.check_password(
            password
        )  # always hash, so locked and open accounts take equal time
        if user.locked_until and user.locked_until > t:
            audit.record("auth.login_blocked", user, user, {"reason": "locked"}, request)
            return _Outcome(locked_minutes=minutes_left(user.locked_until, t))
        if not password_ok:
            locked = record_failure(user, request, "auth.login_failed", "bad_password", t)
            if locked:
                return _Outcome(locked_minutes=locked)
            return _Outcome(failed=True, breakglass_failed=user.login_id if user.is_break_glass else "")
        user.failed_logins = 0
        user.locked_until = None
        user.last_login_at = t
        user.save(update_fields=["failed_logins", "locked_until", "last_login_at"])
        audit.record("auth.login_ok", user, user, {"break_glass": user.is_break_glass}, request)
        return _Outcome(user=user)


def check_password_login(login_id: str, password: str, request: Any = None) -> User:
    """Return the user if the password is right; raise Locked (423) or ValidationFailed (generic)."""
    out = _attempt(login_id, password, request)
    if out.user is not None:
        if out.user.is_break_glass:
            alerts.raise_alert(
                "auth.breakglass_used",
                "CRITICAL",
                "Break-glass account was used to log in",
                f"login {out.user.login_id} at {now().isoformat()}",
            )
        return out.user
    if out.breakglass_failed:
        alerts.raise_alert(
            "auth.breakglass_failed",
            "CRITICAL",
            "Wrong password tried on the break-glass account",
            f"login {out.breakglass_failed} at {now().isoformat()}",
        )
    if out.locked_minutes:
        raise Locked(
            f"Too many wrong attempts. Try again in {out.locked_minutes} minute(s).",
            minutes=out.locked_minutes,
        )
    raise ValidationFailed(GENERIC_FAIL, fields={"password": [GENERIC_FAIL]})
