"""Starting a work session: the one place a login completes once the selfie stage is on."""

from __future__ import annotations

from typing import Any

from django.contrib.auth import login
from django.core import signing
from django.db import transaction

from ots.audit import api as audit
from ots.core import events
from ots.core.clock import now, today_local
from ots.core.errors import ValidationFailed
from ots.core.ids import uuid7

from . import selfies
from .models import IdleMode, WorkSession

PENDING_KEY = "pending_login"
PENDING_MAX_AGE = 180  # seconds (M06 section 1)
BACKEND = "django.contrib.auth.backends.ModelBackend"
_SALT = "ots.attendance.pending-login"


def make_pending(request: Any, user: Any) -> None:
    """Stage 1 is done (the password was right). No session exists yet, only this signed, short-lived note."""
    request.session.flush()
    request.session[PENDING_KEY] = signing.dumps({"u": str(user.pk)}, salt=_SALT)


def pending_user(request: Any) -> Any | None:
    from ots.accounts.models import User

    token = request.session.get(PENDING_KEY)
    if not token:
        return None
    try:
        data = signing.loads(token, salt=_SALT, max_age=PENDING_MAX_AGE)
    except signing.BadSignature:
        return None
    return User.objects.filter(pk=data["u"], is_active=True).first()


def _clean_device(raw: str) -> str:
    return "".join(c for c in raw if c.isalnum() or c == "-")[:64]


@transaction.atomic
def start_session(
    request: Any,
    user: Any,
    selfie: bytes,
    *,
    device_id: str = "",
    key: Any = None,
    idle_mode: str = IdleMode.TAB_ONLY,
) -> WorkSession:
    """Check the picture, then in one transaction: the Django session, the work session, the audit row and the
    `session.started` event. The picture file is written before the commit and swapped in after it."""
    jpeg = selfies.process(selfie)
    session_id = uuid7()
    at = now()
    day = today_local()
    rel = selfies.relative_path(session_id, day)
    selfies.stage(rel, jpeg)
    try:
        request.session.pop(PENDING_KEY, None)
        login(request, user, backend=BACKEND)
        request.session.save()
        ws = WorkSession.objects.create(
            id=session_id,
            user=user,
            work_date=day,
            login_at=at,
            selfie_path=rel,
            idle_mode=idle_mode,
            client_ip=request.META.get("REMOTE_ADDR") or None,
            user_agent=request.META.get("HTTP_USER_AGENT", "")[:300],
            device_id=_clean_device(device_id),
            session_key=request.session.session_key or "",
            login_key=key,
        )
        audit.record("auth.session_started", user, ws, {"idle_mode": idle_mode}, request)
        events.publish("session.started", ws, {"user_id": str(user.pk), "work_date": str(day)})
    except Exception:
        selfies.discard(rel)
        raise
    transaction.on_commit(lambda: selfies.publish(rel))
    return ws


CAMERA_REASONS = ("denied", "nocamera", "busy", "insecure", "error")


def selfie_unavailable(request: Any, user: Any, reason: str = "error") -> None:
    """The browser could not use the camera (F-AT-3): no login, and it is written down with the reason."""
    reason = reason if reason in CAMERA_REASONS else "error"
    with transaction.atomic():
        audit.record("auth.selfie_unavailable", user, user, {"reason": reason}, request)


def existing_for_key(user: Any, key: Any) -> WorkSession | None:
    if key is None:
        return None
    return WorkSession.objects.filter(user=user, login_key=key).first()


def check_key(raw: str | None) -> Any:
    import uuid

    if not raw:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise ValidationFailed("Bad request key.") from None
