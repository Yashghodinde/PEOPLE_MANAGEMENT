"""Password login: Argon2id check, lockout (5 failures / 15 min -> 15 min lock), audit of every attempt.

Audit rows for failures must survive the failure, so the attempt is decided inside one transaction and
the error is raised only after it has committed.
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
_DUMMY_HASH: str | None = None


@dataclass
class _Outcome:
    user: User | None = None
    locked_minutes: int = 0
    failed: bool = False


def _burn_time(password: str) -> None:
    """Spend the same hashing time for unknown users, so response time does not reveal valid IDs."""
    global _DUMMY_HASH  # noqa: PLW0603
    if _DUMMY_HASH is None:
        _DUMMY_HASH = make_password("not-a-real-password")
    check_password(password, _DUMMY_HASH)


def _minutes_left(until: datetime.datetime, t: datetime.datetime) -> int:
    return max(1, math.ceil((until - t).total_seconds() / 60))


def _attempt(login_id: str, password: str, request: Any) -> _Outcome:
    t = now()
    with transaction.atomic():
        user = User.objects.select_for_update().filter(login_id__iexact=login_id.strip()).first()
        if user is None or not user.is_active:
            audit.record(
                "auth.login_failed", None, None, {"login_id": login_id[:60], "reason": "unknown"}, request
            )
            if user is None:
                _burn_time(password)
            return _Outcome(failed=True)
        if user.locked_until and user.locked_until > t:
            audit.record("auth.login_blocked", user, user, {"reason": "locked"}, request)
            return _Outcome(locked_minutes=_minutes_left(user.locked_until, t))
        if not user.check_password(password):
            audit.record("auth.login_failed", user, user, {"reason": "bad_password"}, request)
            recent = AuditLog.objects.filter(
                action="auth.login_failed", target_type="user", target_id=str(user.pk), at__gte=t - WINDOW
            ).count()
            user.failed_logins = recent
            if recent >= MAX_FAILURES:
                user.locked_until = t + LOCK_FOR
                audit.record("auth.locked", user, user, {"until": user.locked_until.isoformat()}, request)
                user.save(update_fields=["failed_logins", "locked_until"])
                return _Outcome(locked_minutes=_minutes_left(user.locked_until, t))
            user.save(update_fields=["failed_logins"])
            return _Outcome(failed=True)
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
    if out.locked_minutes:
        raise Locked(
            f"Too many wrong attempts. Try again in {out.locked_minutes} minute(s).",
            minutes=out.locked_minutes,
        )
    raise ValidationFailed(GENERIC_FAIL, fields={"password": [GENERIC_FAIL]})
