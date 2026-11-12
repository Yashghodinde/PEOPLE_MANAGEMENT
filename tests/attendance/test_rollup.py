"""M06.4: roll-up statuses, corrections and holidays."""

import datetime

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from freezegun import freeze_time
from ots.accounts.models import Role
from ots.attendance import corrections, rollup, sessions
from ots.attendance.models import AttendanceCorrection, AttendanceDay, Holiday
from ots.core.errors import NotFound, PermissionDenied, ValidationFailed

from tests.accounts.factories import make_user

pytestmark = pytest.mark.django_db
DAY = datetime.date(2026, 11, 12)  # a Thursday
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))


def at(h, m=0, day=DAY):
    return datetime.datetime(day.year, day.month, day.day, h, m, tzinfo=IST)


def status(user, day=DAY):
    rollup.rollup(day, [user.pk])
    return AttendanceDay.objects.get(user=user, date=day)


def test_present_late_absent(worker, make_ws):
    assert status(worker).status == "A"
    ws = make_ws(worker, work_date=DAY, login_at=at(9, 35))  # shift starts 09:30, grace 10 minutes
    assert status(worker).status == "P"
    ws.login_at = at(9, 41)
    ws.save()
    row = status(worker)
    assert row.status == "L" and row.late_minutes == 11 and row.sessions == 1


def test_weekly_off_and_holiday(worker, make_ws):
    sunday = DAY + datetime.timedelta(days=3)
    assert status(worker, sunday).status == "WO"  # default weekly off includes Sunday
    Holiday.objects.create(company=worker.company, date=DAY, name="Diwali")
    assert status(worker).status == "H"


def test_the_row_carries_the_session_figures(worker, make_ws):
    with freeze_time(at(10)):
        a = make_ws(worker, work_date=DAY, login_at=at(9, 30))
        sessions.end_current(a.session_key, "MANUAL")
    with freeze_time(at(11)):
        b = make_ws(worker, work_date=DAY, login_at=at(10, 30), live_connected=True)
        sessions.report_state(b.session_key, "idle")
    with freeze_time(at(12)):
        sessions.sweep()  # closes b as IDLE_45
    row = status(worker)
    assert row.sessions == 2 and row.idle_logouts == 1
    assert row.first_login == at(9, 30) and row.last_logout is not None and row.active_minutes >= 29


def test_running_it_again_changes_nothing(worker, make_ws):
    make_ws(worker, work_date=DAY, login_at=at(9, 30))
    first = status(worker)
    again = status(worker)
    assert AttendanceDay.objects.count() == 1 and first.status == again.status


def test_nobody_is_counted_before_they_joined(worker):
    worker.date_of_joining = DAY + datetime.timedelta(days=5)
    worker.save()
    rollup.rollup(DAY, [worker.pk])
    assert not AttendanceDay.objects.filter(user=worker).exists()


def test_rollup_for_30_people_is_five_queries_or_fewer(worker, make_ws):
    boss = worker.reports_to
    for i in range(29):
        u = make_user(Role.EMPLOYEE, reports_to=boss)
        if i % 2:
            make_ws(u, work_date=DAY, login_at=at(9, 30))
    with CaptureQueriesContext(connection) as q:
        assert rollup.rollup(DAY) == 31
    assert len(q) <= 5, [x["sql"][:60] for x in q.captured_queries]


def test_a_correction_changes_the_effective_status_but_keeps_the_original(worker):
    boss = worker.reports_to
    rollup.rollup(DAY, [worker.pk])
    c = corrections.request_correction(boss, worker.pk, DAY, "P", "Worked from the client site")
    assert c.approved_at is not None  # the Super Admin's own request needs no second step
    out = rollup.effective([worker.pk], DAY, DAY)[(worker.pk, DAY)]
    assert out["status"] == "P" and out["original"] == "A" and out["corrected"]
    assert AttendanceDay.objects.get(user=worker, date=DAY).status == "A"  # the roll-up itself is untouched


def test_a_sub_admin_asks_and_only_the_super_admin_approves(worker):
    boss = worker.reports_to
    mgr = make_user(Role.SUB_ADMIN, reports_to=boss, login_id="mgr")
    emp = make_user(Role.EMPLOYEE, reports_to=mgr, login_id="e1")
    c = corrections.request_correction(mgr, emp.pk, DAY, "P", "Power cut at the office")
    assert c.approved_at is None
    assert rollup.effective([emp.pk], DAY, DAY).get((emp.pk, DAY), {}).get("corrected") is not True
    with pytest.raises(PermissionDenied):
        corrections.approve_correction(mgr, c.pk)
    corrections.approve_correction(boss, c.pk)
    corrections.approve_correction(boss, c.pk)  # twice: nothing more happens
    assert rollup.effective([emp.pk], DAY, DAY)[(emp.pk, DAY)]["status"] == "P"
    assert AttendanceCorrection.objects.count() == 1


def test_a_sub_admin_cannot_correct_someone_outside_their_team(worker):
    boss = worker.reports_to
    mgr = make_user(Role.SUB_ADMIN, reports_to=boss, login_id="mgr")
    with pytest.raises((PermissionDenied, NotFound)):
        corrections.request_correction(mgr, worker.pk, DAY, "P", "Not my person at all")
    with pytest.raises(PermissionDenied):
        corrections.request_correction(worker, worker.pk, DAY, "P", "I would like to be present")


def test_correction_input_is_checked(worker):
    boss = worker.reports_to
    with pytest.raises(ValidationFailed):
        corrections.request_correction(boss, worker.pk, DAY, "P", "no")
    with pytest.raises(ValidationFailed):
        corrections.request_correction(boss, worker.pk, DAY, "XX", "A fine reason here")
    with pytest.raises(ValidationFailed):
        corrections.request_correction(boss, worker.pk, datetime.date(2099, 1, 1), "P", "A fine reason here")


def test_holidays_are_super_admin_only_and_update_the_roll_up(worker):
    boss = worker.reports_to
    rollup.rollup(DAY, [worker.pk])
    with pytest.raises(PermissionDenied):
        corrections.add_holiday(worker, DAY, "Nope")
    h = corrections.add_holiday(boss, DAY, "Diwali")
    assert AttendanceDay.objects.get(user=worker, date=DAY).status == "H"
    corrections.remove_holiday(boss, h.pk)
    assert AttendanceDay.objects.get(user=worker, date=DAY).status == "A"
