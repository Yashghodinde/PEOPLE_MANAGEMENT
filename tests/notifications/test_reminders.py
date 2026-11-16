"""M10.2: deadline reminders (unique per assignment and window), alert notifications, F-NT-1."""

import datetime

import pytest
from django.db import IntegrityError, transaction
from freezegun import freeze_time
from ots.accounts.models import Role
from ots.core.clock import now
from ots.core.events import Event
from ots.notifications import jobs, services, subscribers
from ots.notifications.models import Kind, Notification
from ots.tasks.models import TaskAssignment

from tests.accounts.factories import make_user
from tests.tasks.factories import make_task

pytestmark = pytest.mark.django_db
# 2026-11-16 03:30 UTC is 09:00 in the office
NINE = "2026-11-16 03:30:00"


@pytest.fixture
def team():
    boss = make_user(Role.SUPER_ADMIN, login_id="boss")
    sub = make_user(Role.SUB_ADMIN, reports_to=boss, login_id="sub")
    emp = make_user(Role.EMPLOYEE, reports_to=sub, login_id="emp")
    return {"boss": boss, "sub": sub, "emp": emp}


def reminders(user=None):
    qs = Notification.objects.filter(kind=Kind.REMINDER)
    return qs.filter(user=user) if user else qs


def at(stamp, hours=0, minutes=0):
    t = datetime.datetime.fromisoformat(stamp).replace(tzinfo=datetime.UTC)
    return t + datetime.timedelta(hours=hours, minutes=minutes)


# --- reminders ---------------------------------------------------------------------------------------------------


def test_a_task_due_within_a_day_gets_one_reminder_and_a_rerun_adds_nothing(team):
    with freeze_time(NINE):
        make_task(team["sub"], team["emp"], due_in_hours=20, title="Quarterly filing")
        assert jobs.run() == 1
        assert jobs.run() == 0 and jobs.run() == 0
    (n,) = reminders(team["emp"])
    assert n.title == "Due within 24 hours: Quarterly filing" and n.source.endswith(":24h")
    assert not reminders(team["sub"])  # only the assignee


def test_tightest_window_wins_and_it_is_not_repeated(team):
    with freeze_time(NINE):
        make_task(team["sub"], team["emp"], due_in_hours=1.5)
        assert jobs.run() == 1  # "under 2 hours", not also "within 24 hours"
        assert jobs.run() == 0
    (n,) = reminders(team["emp"])
    assert "in under 2 hours" in n.title and n.source.endswith(":2h")


def test_a_later_run_in_the_same_window_adds_nothing_but_the_next_window_does(team):
    with freeze_time(NINE):
        t = make_task(team["sub"], team["emp"], due_in_hours=21)
        jobs.run()
    with freeze_time(at(NINE, hours=1)):  # still 20 h to go: the 24 h window again
        assert jobs.run() == 0
    with freeze_time(at(NINE, hours=19.5)):  # 1.5 h to go, but the office is closed (23:00 IST)
        assert jobs.run() == 0
    t.due_at = at(NINE, hours=1)
    t.save()
    with freeze_time(NINE):
        assert jobs.run() == 1  # now inside two hours: the second, tighter reminder
    assert sorted(n.source.rsplit(":", 1)[1] for n in reminders(team["emp"])) == ["24h", "2h"]


def test_overdue_work_is_mentioned_once_a_day_from_ten(team):
    with freeze_time("2026-11-16 03:30:00"):
        make_task(team["sub"], team["emp"], due_in_hours=2, title="Late job")
    with freeze_time("2026-11-16 08:00:00"):  # 13:30 IST, due 11:00 IST: overdue
        assert jobs.run() == 1
        assert jobs.run() == 0
    with freeze_time("2026-11-17 03:00:00"):  # 08:30 IST next day: before ten
        assert jobs.run() == 0
    with freeze_time("2026-11-17 04:45:00"):  # 10:15 IST
        assert jobs.run() == 1
        assert jobs.run() == 0
    titles = list(reminders(team["emp"]).order_by("created_at").values_list("title", flat=True))
    assert titles == ["Overdue: Late job", "Overdue: Late job"]


def test_nothing_goes_out_at_night(team):
    with freeze_time(NINE):
        make_task(team["sub"], team["emp"], due_in_hours=1.5)
    with freeze_time("2026-11-16 02:00:00"):  # 07:30 IST
        assert jobs.run() == 0
    with freeze_time("2026-11-16 15:00:00"):  # 20:30 IST
        assert jobs.run() == 0
    with freeze_time("2026-11-16 14:30:00"):  # 20:00 IST sharp is still inside
        assert jobs.run() >= 0


@pytest.mark.parametrize("closer", ["cancelled", "removed", "approved"])
def test_finished_cancelled_or_removed_work_is_never_reminded(team, closer):
    with freeze_time(NINE):
        t = make_task(team["sub"], team["emp"], due_in_hours=1.5)
        a = TaskAssignment.objects.get(task=t)
        if closer == "cancelled":
            t.is_cancelled = True
            t.save()
        elif closer == "removed":
            a.removed_at = now()
            a.save()
        else:
            a.status = "APPROVED"
            a.save()
        assert jobs.run() == 0


def test_the_reminder_job_is_registered_every_fifteen_minutes():
    from ots.core.jobs import registered_jobs

    j = registered_jobs()["notifications.deadline_reminders"]
    assert j.schedule == "*/15 8-20 * * *"


# --- F-NT-1 and alerts -------------------------------------------------------------------------------------------


def alert_event(severity="CRITICAL", event_id="e1"):
    return Event(id=event_id, type="alert.raised", aggregate_type="alert", aggregate_id="k",
                 payload={"key": "backup.stale", "severity": severity, "title": "Backups are late"}, at=now())  # fmt: skip


def test_a_critical_alert_reaches_every_super_admin_only(team):
    boss2 = make_user(Role.SUPER_ADMIN, login_id="boss2")
    subscribers.alert_raised(alert_event())
    got = Notification.objects.filter(kind=Kind.ALERT)
    assert {n.user_id for n in got} == {team["boss"].pk, boss2.pk}
    assert all(n.task_id is None and n.title == "Backups are late" and n.body == "backup.stale" for n in got)
    subscribers.alert_raised(alert_event("WARNING", "e2"))
    assert Notification.objects.filter(kind=Kind.ALERT).count() == 2


def test_an_alert_notification_opens_and_clears_without_a_task(team):
    from django.test import Client

    subscribers.alert_raised(alert_event())
    c = Client()
    assert c.post("/login", {"login_id": "boss", "password": "correct horse battery"}).status_code == 302
    assert b"Backups are late" in c.get("/app/notifications").content
    n = Notification.objects.get(user=team["boss"])
    r = c.post(f"/app/notifications/{n.pk}/open")
    assert r.status_code == 302 and r["Location"] == "/app/notifications"
    assert c.get("/app/tasks").status_code == 200  # the dots and 'new for you' ignore task-less rows


def test_a_handler_run_twice_for_one_event_makes_one_notification(team):  # F-NT-1
    subscribers.alert_raised(alert_event())
    subscribers.alert_raised(alert_event())
    assert Notification.objects.filter(kind=Kind.ALERT, user=team["boss"]).count() == 1
    subscribers.alert_raised(alert_event(event_id="e2"))  # a different event is a different cause
    assert Notification.objects.filter(kind=Kind.ALERT, user=team["boss"]).count() == 2


def test_the_database_itself_refuses_a_duplicate_cause(team):  # F-NT-1 when two handlers race
    task = make_task(team["sub"], team["emp"])
    services.notify([team["emp"].pk], Kind.CHANGED, task, "x", source="cause-1")
    with pytest.raises(IntegrityError), transaction.atomic():
        Notification.objects.create(
            user=team["emp"], kind=Kind.CHANGED, task=task, title="x", source="cause-1"
        )
    Notification.objects.create(
        user=team["emp"], kind=Kind.CHANGED, task=task, title="y", source=""
    )  # no source
    Notification.objects.create(user=team["emp"], kind=Kind.CHANGED, task=task, title="z", source="")
