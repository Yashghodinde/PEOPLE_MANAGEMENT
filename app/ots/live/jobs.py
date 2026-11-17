"""Worker jobs for the live channel."""

from __future__ import annotations

import datetime

from ots.core import alerts
from ots.core.clock import now
from ots.core.jobs import job
from ots.core.models import OutboxEvent

LAG_LIMIT = datetime.timedelta(seconds=60)


def check_lag() -> bool:
    """F-LV-1: events waiting more than 60 s mean the dispatcher is stuck. Returns True when lagging."""
    oldest = (
        OutboxEvent.objects.filter(published_at__isnull=True)
        .order_by("at")
        .values_list("at", flat=True)
        .first()
    )
    if oldest is not None and now() - oldest > LAG_LIMIT:
        alerts.raise_alert(
            "live.lag",
            "WARNING",
            "Live updates are delayed",
            f"The oldest waiting event is from {oldest.isoformat()}",
        )
        return True
    alerts.resolve_alert("live.lag")
    return False


@job("live.lag_check", schedule="* * * * *")
def lag_check() -> None:
    check_lag()


@job("live.display_reload", schedule="0 3 * * *")
def display_reload() -> None:
    """03:00: every office TV reloads its page, so a long-running browser starts fresh each day (F-TV-1)."""
    from .publisher import send

    send(roles=["DISPLAY"], type_="reload")
