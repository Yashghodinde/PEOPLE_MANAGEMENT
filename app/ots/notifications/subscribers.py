"""Notifications that come from events rather than from a task action (M10 section 2)."""

from __future__ import annotations

from ots.accounts.models import Role, User
from ots.core.events import Event, subscribe

from . import services
from .models import Kind


@subscribe("alert.raised")
def alert_raised(event: Event) -> None:
    """A CRITICAL alert reaches every Super Admin's bell; `source` makes a retried delivery a no-op."""
    p = event.payload
    if p.get("severity") != "CRITICAL":
        return
    admins = User.objects.filter(role=Role.SUPER_ADMIN, is_active=True).values_list("pk", flat=True)
    services.notify(
        admins, Kind.ALERT, None, str(p.get("title", "System alert"))[:200], body=str(p.get("key", "")),
        source=f"event:{event.id}",
    )  # fmt: skip
