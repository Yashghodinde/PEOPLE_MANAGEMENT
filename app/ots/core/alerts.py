"""Alerts: one row per stable key. Raising again bumps the counter; resolving closes it; a resolved key that
fires again is reopened as a new incident. Events are published on transitions only (new, reopened,
severity change, resolved) so a once-a-minute watchdog does not flood the outbox."""

from django.db import transaction
from django.db.models import QuerySet

from . import events
from .clock import now
from .models import Alert


def raise_alert(key: str, severity: str, title: str, detail: str = "") -> Alert:
    if severity not in Alert.Severity.values:
        raise ValueError(f"unknown severity: {severity}")
    t = now()
    with transaction.atomic():
        alert, created = Alert.objects.select_for_update().get_or_create(
            key=key,
            defaults={
                "severity": severity,
                "title": title,
                "detail": detail,
                "first_seen_at": t,
                "last_seen_at": t,
            },
        )
        changed = created
        if not created:
            reopened = alert.resolved_at is not None
            changed = reopened or alert.severity != severity
            if reopened:
                alert.resolved_at = None
                alert.first_seen_at = t
            alert.severity, alert.title, alert.detail = severity, title, detail
            alert.count += 1
            alert.last_seen_at = t
            alert.save()
        if changed:
            events.publish("alert.raised", ("alert", key), {"key": key, "severity": severity, "title": title})
    return alert


def resolve_alert(key: str) -> bool:
    """Close an open alert. Returns True if one was open."""
    with transaction.atomic():
        alert = Alert.objects.select_for_update().filter(key=key, resolved_at__isnull=True).first()
        if alert is None:
            return False
        alert.resolved_at = now()
        alert.save(update_fields=["resolved_at"])
        events.publish("alert.resolved", ("alert", key), {"key": key, "severity": alert.severity})
    return True


def open_alerts(severity: str | None = None) -> QuerySet[Alert]:
    qs = Alert.objects.filter(resolved_at__isnull=True)
    return qs.filter(severity=severity) if severity else qs
