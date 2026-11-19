"""M06.5: calendar, day drawer, selfie permissions, team widget, timeline."""

import datetime
import uuid

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from ots.accounts.models import Role
from ots.attendance import rollup
from ots.audit.models import AuditLog
from ots.core.clock import today_local

from tests.accounts.factories import make_user

pytestmark = pytest.mark.django_db
PW = "correct horse battery"
TODAY = today_local()


@pytest.fixture(autouse=True)
def selfie_stage_on(settings):
    """These tests log in with a password only (the selfie step is covered in test_selfie_login)."""
    settings.OTS_SELFIE_STAGE_ENABLED = False


def login(login_id):
    c = Client()
    assert c.post("/login", {"login_id": login_id, "password": PW}).status_code == 302
    return c


@pytest.fixture
def team(make_ws):
    boss = make_user(Role.SUPER_ADMIN, login_id="boss")
    mgr = make_user(Role.SUB_ADMIN, reports_to=boss, login_id="mgr")
    other = make_user(Role.SUB_ADMIN, reports_to=boss, login_id="other")
    emp = make_user(Role.EMPLOYEE, reports_to=mgr, login_id="emp")
    peer = make_user(Role.EMPLOYEE, reports_to=mgr, login_id="peer")
    away = make_user(Role.EMPLOYEE, reports_to=other, login_id="away")
    ws = make_ws(emp, selfie_path="x.jpg")
    return {"boss": boss, "mgr": mgr, "other": other, "emp": emp, "peer": peer, "away": away, "ws": ws}


def test_an_employee_sees_only_their_own_calendar(team):
    c = login("emp")
    assert c.get("/app/attendance").status_code == 302
    assert c.get(f"/app/attendance/{team['emp'].pk}").status_code == 200
    assert c.get(f"/app/attendance/{team['peer'].pk}").status_code == 404
    assert c.get(f"/app/attendance/{team['boss'].pk}").status_code == 404


def test_a_sub_admin_sees_their_team_and_not_others(team):
    c = login("mgr")
    assert c.get(f"/app/attendance/{team['emp'].pk}").status_code == 200
    assert c.get(f"/app/attendance/{team['away'].pk}").status_code == 404
    assert login("boss").get(f"/app/attendance/{team['away'].pk}").status_code == 200


def test_bad_ids_and_months_do_not_crash(team):
    c = login("boss")
    assert c.get("/app/attendance/not-a-uuid").status_code == 404
    for q in ("month=zzz", "month=2026-13", "day=nope", "month=0000-01", "day=2026-02-30"):
        assert c.get(f"/app/attendance/{team['emp'].pk}?{q}").status_code == 200


def test_the_grid_shows_today_as_present_and_a_drawer_with_the_session(team):
    c = login("emp")
    body = c.get(f"/app/attendance/{team['emp'].pk}?day={TODAY.isoformat()}").content.decode()
    assert "ring-accent" in body and "active minutes" in body and "In " in body
    assert f"/app/attendance/selfie/{team['ws'].pk}" in body  # their own picture


def test_selfies_are_for_the_super_admin_and_the_person_only(team, settings):
    path = settings.OTS_SELFIE_DIR / "x.jpg"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xd8\xff fake")
    url = f"/app/attendance/selfie/{team['ws'].pk}"
    assert login("emp").get(url).status_code == 200
    assert login("boss").get(url).status_code == 200
    assert AuditLog.objects.filter(action="selfie.viewed").count() == 2
    assert login("mgr").get(url).status_code == 403  # their own manager may not
    assert login("peer").get(url).status_code == 403
    assert login("boss").get(f"/app/attendance/selfie/{uuid.uuid4()}").status_code == 404
    assert "no-store" in login("emp").get(url)["Cache-Control"]
    body = login("mgr").get(f"/app/attendance/{team['emp'].pk}?day={TODAY.isoformat()}").content.decode()
    assert "attendance/selfie/" not in body  # no thumbnail for a manager


def test_corrections_flow_through_the_screens(team):
    day = (TODAY - datetime.timedelta(days=2)).isoformat()
    data = {"date": day, "new_status": "P", "reason": "Visited the client"}
    assert login("emp").post(f"/app/attendance/{team['emp'].pk}/correction", data).status_code in (403, 404)
    assert login("mgr").post(f"/app/attendance/{team['emp'].pk}/correction", data).status_code == 302
    assert login("mgr").get("/app/attendance/corrections").status_code == 403
    page = login("boss").get("/app/attendance/corrections").content.decode()
    assert "Visited the client" in page
    from ots.attendance.models import AttendanceCorrection

    c = AttendanceCorrection.objects.get()
    assert login("mgr").post(f"/app/attendance/corrections/{c.pk}/approve").status_code == 403
    assert login("boss").post(f"/app/attendance/corrections/{c.pk}/approve").status_code == 302
    month = day[:7]
    body = login("emp").get(f"/app/attendance/{team['emp'].pk}?month={month}&day={day}").content.decode()
    assert "Correction to Present" in body
    bad = login("mgr").post(f"/app/attendance/{team['emp'].pk}/correction", {**data, "reason": "x"})
    assert bad.status_code == 422


def test_holidays_page_is_for_the_super_admin(team):
    assert login("mgr").get("/app/attendance/holidays").status_code == 403
    c = login("boss")
    assert c.post("/app/attendance/holidays", {"date": "2026-12-25", "name": "Christmas"}).status_code == 302
    assert "Christmas" in c.get("/app/attendance/holidays").content.decode()
    from ots.attendance.models import Holiday

    assert c.post("/app/attendance/holidays", {"remove": str(Holiday.objects.get().pk)}).status_code == 302
    assert Holiday.objects.count() == 0
    assert c.post("/app/attendance/holidays", {"date": "bad", "name": "x"}).status_code == 422


def test_force_logout_from_the_drawer(team):
    url = f"/app/attendance/sessions/{team['ws'].pk}/force-logout"
    assert login("mgr").post(url, {"reason": "Left unlocked"}).status_code == 403
    assert login("boss").post(url, {"reason": "no"}).status_code == 422
    assert login("boss").post(url, {"reason": "Left it unlocked"}).status_code == 302
    team["ws"].refresh_from_db()
    assert team["ws"].logout_reason == "FORCED"


def test_team_today_widget_and_scope(team):
    body = login("mgr").get("/app/dashboard").content.decode()
    assert "Team today" in body
    assert team["peer"].full_name in body and team["away"].full_name not in body
    assert "Team today" not in login("emp").get("/app/dashboard").content.decode()


def test_drawer_and_calendar_query_counts(team):
    c = login("boss")
    with CaptureQueriesContext(connection) as q:
        assert c.get(f"/app/attendance/{team['emp'].pk}?day={TODAY.isoformat()}").status_code == 200
    assert len(q) <= 17, len(q)
    with CaptureQueriesContext(connection) as q:
        rollup.rollup(TODAY)
    assert len(q) <= 6


def test_sessions_and_tasks_show_in_the_activity_timeline(team):
    from ots.audit import timeline

    assert {"session", "task"} <= set(timeline.providers())
    events = timeline.activity_timeline(team["emp"].pk).events
    assert any(e.source == "session" for e in events)


def test_team_today_does_not_add_queries_per_person(team):
    c = login("boss")

    def count():
        with CaptureQueriesContext(connection) as q:
            c.get("/app/dashboard")
        return len(q)

    before = count()
    for i in range(15):
        make_user(Role.EMPLOYEE, reports_to=team["mgr"], login_id=f"more{i}")
    assert count() == before
