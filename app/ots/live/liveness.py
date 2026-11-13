"""Who is connected: the work session's `live_connected` flag (M06 section 2). The only writer of it."""

from __future__ import annotations

from asgiref.sync import sync_to_async

from ots.core.clock import now


def _set(session_key: str, connected: bool) -> int:
    from ots.attendance.models import WorkSession

    if not session_key:
        return 0
    qs = WorkSession.objects.filter(session_key=session_key, logout_at__isnull=True)
    if connected:
        return qs.update(live_connected=True, live_disconnected_at=None)
    return qs.update(live_connected=False, live_disconnected_at=now())


async def connected(session_key: str) -> int:
    return await sync_to_async(_set)(session_key, True)


async def disconnected(session_key: str) -> int:
    return await sync_to_async(_set)(session_key, False)


def _boot_reset() -> int:
    from ots.attendance.sessions import reset_live_on_boot

    return reset_live_on_boot()


async def boot_reset() -> int:
    """Once per app process, before its first connection is counted (F-AT-5)."""
    return await sync_to_async(_boot_reset)()
