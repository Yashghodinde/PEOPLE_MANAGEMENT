from datetime import timedelta

import pytest
from ots.core import alerts, dispatcher, jobs
from ots.core.clock import now
from ots.core.models import Alert, OutboxEvent

pytestmark = pytest.mark.django_db


def event_types():
    return list(OutboxEvent.objects.order_by("id").values_list("type", flat=True))


def test_first_raise_creates_alert_and_event():
    a = alerts.raise_alert("backup.rpo", "WARNING", "RPO high", "11 min")
    assert (a.count, a.resolved_at) == (1, None) and a.first_seen_at == a.last_seen_at
    assert event_types() == ["alert.raised"]


def test_repeat_raise_dedupes_increments_count_and_does_not_spam_events():
    alerts.raise_alert("k", "WARNING", "t", "d1")
    a = alerts.raise_alert("k", "WARNING", "t", "d2")
    assert Alert.objects.count() == 1 and a.count == 2 and a.detail == "d2"
    assert event_types() == ["alert.raised"]  # no second event for an unchanged open alert


def test_severity_change_republishes():
    alerts.raise_alert("k", "WARNING", "t")
    alerts.raise_alert("k", "CRITICAL", "t")
    assert Alert.objects.get(key="k").severity == "CRITICAL"
    assert event_types() == ["alert.raised", "alert.raised"]


def test_resolve_closes_and_is_idempotent():
    alerts.raise_alert("k", "CRITICAL", "t")
    assert alerts.resolve_alert("k") is True
    assert alerts.resolve_alert("k") is False
    assert Alert.objects.get(key="k").resolved_at is not None
    assert event_types() == ["alert.raised", "alert.resolved"]
    assert list(alerts.open_alerts()) == []


def test_reopen_after_resolve_starts_a_new_incident():
    first = alerts.raise_alert("k", "WARNING", "t")
    alerts.resolve_alert("k")
    Alert.objects.filter(key="k").update(first_seen_at=now() - timedelta(days=2))
    again = alerts.raise_alert("k", "WARNING", "t")
    assert again.resolved_at is None and again.count == first.count + 1
    assert now() - again.first_seen_at < timedelta(seconds=5)  # duration restarts
    assert event_types() == ["alert.raised", "alert.resolved", "alert.raised"]


def test_unknown_severity_rejected():
    with pytest.raises(ValueError):
        alerts.raise_alert("k", "PANIC", "t")


def test_open_alerts_filter_by_severity():
    alerts.raise_alert("a", "INFO", "t")
    alerts.raise_alert("b", "CRITICAL", "t")
    assert [x.key for x in alerts.open_alerts("CRITICAL")] == ["b"]


def test_failure_hooks_are_wired_to_alerts_api():
    assert dispatcher.alert_hook is alerts.raise_alert
    assert jobs.alert_hook is alerts.raise_alert and jobs.resolve_hook is alerts.resolve_alert
