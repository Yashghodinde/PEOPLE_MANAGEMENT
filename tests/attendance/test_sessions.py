"""M06.2: idle, closing and the sweep (F-AT-1, F-AT-2, F-AT-4, F-AT-5)."""

import datetime

import pytest
from django.contrib.sessions.backends.db import SessionStore
from django.test import Client
from freezegun import freeze_time
from ots.attendance import sessions
from ots.attendance.models import WorkSession
from ots.audit.models import AuditLog
from ots.core.models import OutboxEvent

pytestmark = pytest.mark.django_db
T0 = datetime.datetime(2026, 11, 12, 4, 0, tzinfo=datetime.UTC)  # 09:30 in Kolkata


def minutes(n):
    return datetime.timedelta(minutes=n)


def test_browser_closed_closes_at_the_disconnect_time_after_two_minutes(worker, make_ws):  # F-AT-1
    with freeze_time(T0):
        ws = make_ws(worker)
    with freeze_time(T0 + minutes(10)):
        ws.live_connected, ws.live_disconnected_at = False, T0 + minutes(10)
        ws.save()
    with freeze_time(
        T0
        + minutes(
            11,
        )
        + datetime.timedelta(seconds=59)
    ):
        assert sessions.sweep() == 0  # not yet two minutes
    with freeze_time(T0 + minutes(12)):
        assert sessions.sweep() == 1
        assert sessions.sweep() == 0  # running again changes nothing
    ws.refresh_from_db()
    assert ws.logout_reason == "BROWSER_CLOSED" and ws.logout_at == T0 + minutes(10)
    assert ws.active_seconds == 600
    assert not SessionStore(session_key=ws.session_key).exists(ws.session_key)  # the login itself is gone
    assert AuditLog.objects.filter(action="auth.session_ended").count() == 1
    assert OutboxEvent.objects.filter(type="session.ended").count() == 1


def test_idle_for_45_minutes_closes_even_if_the_browser_code_is_dead(worker, make_ws):  # F-AT-2
    with freeze_time(T0):
        ws = make_ws(worker)
    with freeze_time(T0 + minutes(20)):
        sessions.report_state(ws.session_key, "idle")
    ws.refresh_from_db()
    assert ws.idle_started_at == T0 + minutes(20) - datetime.timedelta(seconds=60)  # backdated by 60 s
    idle_start = ws.idle_started_at
    with freeze_time(idle_start + minutes(44)):
        assert sessions.sweep() == 0
    with freeze_time(idle_start + minutes(46)):
        assert sessions.sweep() == 1
    ws.refresh_from_db()
    assert ws.logout_reason == "IDLE_45" and ws.logout_at == idle_start + minutes(45)
    assert ws.active_seconds == int((idle_start - T0).total_seconds())  # the idle 45 minutes are not work


def test_coming_back_in_time_keeps_the_session_and_counts_the_idle(worker, make_ws):
    with freeze_time(T0):
        ws = make_ws(worker)
    with freeze_time(T0 + minutes(10)):
        sessions.report_state(ws.session_key, "idle")
    with freeze_time(T0 + minutes(30)):
        assert sessions.report_state(ws.session_key, "active") == {"closed": False}
        assert sessions.sweep() == 0
    ws.refresh_from_db()
    assert ws.idle_seconds_total == 20 * 60 + 60 and ws.active_resumed_at == T0 + minutes(30)
    with freeze_time(T0 + minutes(60)):
        sessions.end_current(ws.session_key, "MANUAL")
    ws.refresh_from_db()
    assert ws.logout_reason == "MANUAL" and ws.active_seconds == 60 * 60 - (20 * 60 + 60)


def test_returning_after_45_minutes_but_before_the_sweep_still_closes(worker, make_ws):
    with freeze_time(T0):
        ws = make_ws(worker)
        sessions.report_state(ws.session_key, "idle")
    with freeze_time(T0 + minutes(50)):
        assert sessions.report_state(ws.session_key, "active") == {"closed": True}
    ws.refresh_from_db()
    assert ws.logout_reason == "IDLE_45"


def test_the_clients_own_time_is_ignored(worker, make_ws):  # F-AT-4
    with freeze_time(T0):
        ws = make_ws(worker)
    c = Client()
    c.force_login(worker)
    ws.session_key = c.session.session_key
    ws.save()
    with freeze_time(T0 + minutes(5)):
        r = c.post("/attendance/idle", {"state": "idle", "at": "1999-01-01T00:00:00Z", "client_time": "1999"})
    assert r.status_code == 200
    ws.refresh_from_db()
    assert ws.idle_started_at == T0 + minutes(5) - datetime.timedelta(seconds=60)


def test_a_short_restart_does_not_close_sessions(worker, make_ws):  # F-AT-5
    with freeze_time(T0):
        ws = make_ws(worker)
    with freeze_time(T0 + minutes(30)):
        assert sessions.reset_live_on_boot() == 1  # the restart: nobody is connected yet
    with freeze_time(T0 + minutes(30) + datetime.timedelta(seconds=20)):
        WorkSession.objects.filter(pk=ws.pk).update(
            live_connected=True, live_disconnected_at=None
        )  # reconnected
    with freeze_time(T0 + minutes(35)):
        assert sessions.sweep() == 0
    ws.refresh_from_db()
    assert ws.logout_at is None


def test_a_restart_with_no_reconnection_closes_at_the_boot_time(worker, make_ws):  # F-AT-5
    with freeze_time(T0):
        ws = make_ws(worker)
    with freeze_time(T0 + minutes(30)):
        sessions.reset_live_on_boot()
    with freeze_time(T0 + minutes(33)):
        assert sessions.sweep() == 1
    ws.refresh_from_db()
    assert ws.logout_reason == "BROWSER_CLOSED" and ws.logout_at == T0 + minutes(30)


def test_a_session_that_never_connected_is_left_alone(worker, make_ws):
    with freeze_time(T0):
        make_ws(worker, live_connected=False, live_disconnected_at=None)
    with freeze_time(T0 + minutes(300)):
        assert sessions.sweep() == 0


def test_bad_states_and_modes_are_refused(worker, make_ws):
    ws = make_ws(worker)
    c = Client()
    c.force_login(worker)
    ws.session_key = c.session.session_key
    ws.save()
    assert c.post("/attendance/idle", {"state": "sleeping"}).status_code == 422
    assert c.post("/attendance/mode", {"idle_mode": "bad"}).status_code == 422
    assert c.post("/attendance/mode", {"idle_mode": "OS"}).status_code == 200
    ws.refresh_from_db()
    assert ws.idle_mode == "OS"
    assert Client().post("/attendance/idle", {"state": "idle"}).status_code == 401


def test_forced_logout_needs_a_super_admin_and_a_reason(worker, make_ws):
    from ots.core.errors import PermissionDenied, ValidationFailed

    ws = make_ws(worker)
    boss = worker.reports_to
    with pytest.raises(PermissionDenied):
        sessions.force_logout(worker, ws.pk, "because I want to")
    with pytest.raises(ValidationFailed):
        sessions.force_logout(boss, ws.pk, "no")
    sessions.force_logout(boss, ws.pk, "Left the PC unlocked")
    ws.refresh_from_db()
    assert ws.logout_reason == "FORCED"
    assert AuditLog.objects.filter(action="auth.session_ended", actor_id=boss.pk).exists()


def test_logging_out_closes_the_work_session_with_the_right_reason(worker, make_ws):
    c = Client()
    c.force_login(worker)
    ws = make_ws(worker, session_key=c.session.session_key)
    assert c.post("/logout").status_code == 302
    ws.refresh_from_db()
    assert ws.logout_reason == "MANUAL" and ws.logout_at is not None


def test_the_login_page_explains_why_you_were_logged_out():
    body = Client().get("/login?reason=IDLE_45").content.decode()
    assert "45 minutes" in body
    assert "45 minutes" not in Client().get("/login?reason=nonsense").content.decode()
