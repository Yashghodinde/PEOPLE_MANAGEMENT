"""Idle, closing and sweeping work sessions (M06 section 2). All times come from the server clock."""

from __future__ import annotations

import datetime
from typing import Any

from django.contrib.sessions.backends.db import SessionStore
from django.db import transaction

from ots.audit import api as audit
from ots.core import events, permissions
from ots.core.clock import now
from ots.core.errors import NotFound, ValidationFailed

from .models import IdleMode, SessionEndReason, WorkSession

IDLE_BACKDATE = datetime.timedelta(seconds=60)  # the detector only fires after 60 s of idleness
IDLE_LIMIT = datetime.timedelta(minutes=45)
DISCONNECT_LIMIT = datetime.timedelta(minutes=2)


def open_session(session_key: str) -> WorkSession | None:
    if not session_key:
        return None
    return (
        WorkSession.objects.select_for_update()
        .filter(session_key=session_key, logout_at__isnull=True)
        .first()
    )


# ---- what the browser tells us ----


@transaction.atomic
def report_state(session_key: str, state: str) -> dict[str, Any]:
    """`idle` starts an idle span (backdated 60 s); `active` ends it. The client's own clock is never read."""
    if state not in ("idle", "active"):
        raise ValidationFailed("Unknown state.", fields={"state": ["Use idle or active."]})
    ws = open_session(session_key)
    if ws is None:
        raise NotFound("No open session.")
    at = now()
    if state == "idle":
        if ws.idle_started_at is None or ws.active_resumed_at is not None:  # not already idle
            ws.idle_started_at, ws.active_resumed_at = at - IDLE_BACKDATE, None
            ws.save(update_fields=["idle_started_at", "active_resumed_at"])
        return {"closed": False}
    if ws.idle_started_at is not None and ws.active_resumed_at is None:
        span = at - ws.idle_started_at
        if span >= IDLE_LIMIT:  # the sweep has not run yet: close it exactly as the sweep would
            _close(ws, SessionEndReason.IDLE_45, ws.idle_started_at + IDLE_LIMIT)
            return {"closed": True}
        ws.idle_seconds_total += int(span.total_seconds())
        ws.active_resumed_at = at
        ws.save(update_fields=["idle_seconds_total", "active_resumed_at"])
    return {"closed": False}


@transaction.atomic
def set_mode(session_key: str, mode: str) -> None:
    if mode not in IdleMode.values:
        raise ValidationFailed("Unknown mode.", fields={"idle_mode": ["Invalid."]})
    ws = open_session(session_key)
    if ws is None:
        raise NotFound("No open session.")
    if ws.idle_mode != mode:
        ws.idle_mode = mode
        ws.save(update_fields=["idle_mode"])


# ---- closing ----


def active_seconds(ws: WorkSession, logout_at: datetime.datetime) -> int:
    idle = ws.idle_seconds_total
    if ws.idle_started_at is not None and ws.active_resumed_at is None:  # an open idle span counts to the end
        idle += max(0, int((logout_at - ws.idle_started_at).total_seconds()))
    return max(0, int((logout_at - ws.login_at).total_seconds()) - idle)


def _close(ws: WorkSession, reason: str, at: datetime.datetime, actor: Any = None, note: str = "") -> None:
    """Must run inside a transaction with `ws` locked."""
    at = max(at, ws.login_at)
    ws.logout_at, ws.logout_reason, ws.live_connected = at, reason, False
    ws.active_seconds = active_seconds(ws, at)
    ws.save(update_fields=["logout_at", "logout_reason", "live_connected", "active_seconds"])
    if ws.session_key:
        SessionStore(session_key=ws.session_key).delete()
    audit.record(
        "auth.session_ended",
        actor or ws.user,
        ws,
        {"reason": reason, "active_seconds": ws.active_seconds, "note": note},
    )
    events.publish("session.ended", ws, {"user_id": str(ws.user_id), "reason": reason})


@transaction.atomic
def end_current(session_key: str, reason: str) -> bool:
    """The person logged out (break or end of day). True when an open work session was closed."""
    ws = open_session(session_key)
    if ws is None:
        return False
    _close(ws, reason, now())
    return True


@transaction.atomic
def force_logout(actor: Any, session_id: Any, reason: str) -> WorkSession:
    """A Super Admin ends someone's session (reason required, audited)."""
    permissions.require(actor, "session.force_logout", None)
    reason = (reason or "").strip()
    if len(reason) < 5:
        raise ValidationFailed("Give a reason.", fields={"reason": ["At least 5 characters."]})
    ws = WorkSession.objects.select_for_update().select_related("user").filter(pk=session_id).first()
    if ws is None:
        raise NotFound("Session not found.")
    if ws.logout_at is not None:
        raise ValidationFailed("That session has already ended.")
    _close(ws, SessionEndReason.FORCED, now(), actor=actor, note=reason)
    return ws


# ---- the sweep (every minute) ----


def sweep() -> int:
    """Close sessions the browser cannot or will not close itself. Safe to run any number of times."""
    closed = 0
    at = now()
    candidates = WorkSession.objects.filter(logout_at__isnull=True).values_list("pk", flat=True)
    for pk in list(candidates):
        with transaction.atomic():
            ws = (
                WorkSession.objects.select_for_update()
                .select_related("user")
                .filter(pk=pk, logout_at__isnull=True)
                .first()
            )
            if ws is None:
                continue
            if (
                ws.idle_started_at is not None
                and ws.active_resumed_at is None
                and at - ws.idle_started_at >= IDLE_LIMIT
            ):
                _close(ws, SessionEndReason.IDLE_45, ws.idle_started_at + IDLE_LIMIT)
                closed += 1
            elif (
                not ws.live_connected
                and ws.live_disconnected_at is not None
                and at - ws.live_disconnected_at >= DISCONNECT_LIMIT
            ):
                _close(ws, SessionEndReason.BROWSER_CLOSED, ws.live_disconnected_at)
                closed += 1
    return closed


def reset_live_on_boot(boot: datetime.datetime | None = None) -> int:
    """After a restart nobody is connected yet. Clients reconnect within 30 s, inside the 2-minute rule."""
    boot = boot or now()
    return WorkSession.objects.filter(logout_at__isnull=True, live_connected=True).update(
        live_connected=False, live_disconnected_at=boot
    )
