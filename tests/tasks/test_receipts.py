"""M07.6 and M10.2b: delivery receipts (D-052): saved, delivered, seen, "Not delivered yet", and the 15-minute
notice for urgent changes. Receipts inform; they never block or change a task."""

import datetime
import uuid

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from freezegun import freeze_time
from ots.accounts.models import Role
from ots.audit.models import AuditLog
from ots.core.clock import now
from ots.live import publisher
from ots.notifications.models import Kind, Notification
from ots.tasks import receipts, services
from ots.tasks.models import EventReceipt, Priority

from tests.accounts.factories import make_user

pytestmark = pytest.mark.django_db
PW = "correct horse battery"
T0 = "2026-11-16 05:00:00"


@pytest.fixture
def team():
    boss = make_user(Role.SUPER_ADMIN, login_id="boss")
    sub = make_user(Role.SUB_ADMIN, reports_to=boss, login_id="sub")
    sub2 = make_user(Role.SUB_ADMIN, reports_to=boss, login_id="sub2")
    emp = make_user(Role.EMPLOYEE, reports_to=sub, login_id="emp")
    other = make_user(Role.EMPLOYEE, reports_to=sub2, login_id="other")
    return {"boss": boss, "sub": sub, "sub2": sub2, "emp": emp, "other": other}


def login(login_id):
    c = Client()
    assert c.post("/login", {"login_id": login_id, "password": PW}).status_code == 302
    return c


def new_task(team, priority=Priority.NORMAL, who="emp", **kw):
    creator = team["sub2"] if who == "other" else team["sub"]  # a Sub-Admin gives work to their own team only
    return services.create_task(
        creator, title="Monthly report", due_at=now() + datetime.timedelta(days=2),
        assignee_ids=[team[who].pk], priority=priority, **kw,
    )  # fmt: skip


def ack(c, ids):
    return c.post("/app/receipts/ack", {"ids": [str(i) for i in ids]}, content_type="application/json")


# --- which events make a receipt ------------------------------------------------------------------------------


def test_assigning_a_task_makes_one_receipt_for_the_assignee_only(team):
    task = new_task(team)
    (r,) = EventReceipt.objects.all()
    assert (r.recipient_id, r.sender_id, r.task_id, r.what) == (
        team["emp"].pk,
        team["sub"].pk,
        task.pk,
        "assignment.created",
    )
    assert r.delivered_at is None and r.seen_at is None and not r.urgent


def test_an_ordinary_event_makes_none_but_everything_on_an_urgent_task_does(team):
    task = new_task(team)
    EventReceipt.objects.all().delete()
    services.update_task(team["sub"], task.pk, title="Monthly report v2")
    assert EventReceipt.objects.count() == 0
    urgent = new_task(team, Priority.URGENT)
    EventReceipt.objects.all().delete()
    services.update_task(team["sub"], urgent.pk, title="Urgent report v2")
    (r,) = EventReceipt.objects.all()
    assert r.recipient_id == team["emp"].pk and r.urgent


def test_the_person_who_made_the_change_never_gets_a_receipt(team):
    task = services.create_task(
        team["sub"], title="Self", due_at=now() + datetime.timedelta(days=1), assignee_ids=[team["sub"].pk]
    )
    assert not EventReceipt.objects.filter(task=task).exists()


def test_a_deactivated_person_gets_none_and_the_task_still_saves(team):
    team["emp"].is_active = False
    team["emp"].save()
    from ots.core.errors import OtsError

    try:
        task = new_task(team)
    except OtsError:
        return  # the rule refusing an inactive assignee is F-TK-5; a receipt never decides that
    assert not EventReceipt.objects.filter(task=task).exists()


def test_the_recipients_open_page_is_told_at_once(team, monkeypatch):
    sent = []
    monkeypatch.setattr(publisher, "send", lambda **kw: sent.append(kw))
    new_task(team)
    mine = [m for m in sent if m["type_"] == "receipt"]
    assert len(mine) == 1 and list(mine[0]["users"]) == [team["emp"].pk] and "receipt_id" in mine[0]


# --- acknowledging (F-TK-8) ----------------------------------------------------------------------------------------


def test_the_recipient_acks_and_the_server_stamps_its_own_time(team):
    new_task(team)
    r = EventReceipt.objects.get()
    with freeze_time(T0):
        resp = ack(login("emp"), [r.pk])
    assert resp.status_code == 200 and resp.json() == {"acked": 1}
    r.refresh_from_db()
    assert r.delivered_at == datetime.datetime(2026, 11, 16, 5, 0, tzinfo=datetime.UTC) and r.seen_at is None


def test_the_same_ack_twice_changes_nothing(team):
    new_task(team)
    r = EventReceipt.objects.get()
    c = login("emp")
    with freeze_time(T0):
        ack(c, [r.pk])
    r.refresh_from_db()
    first = r.delivered_at
    with freeze_time("2026-11-16 09:00:00"):
        assert ack(c, [r.pk]).json() == {"acked": 0}
    r.refresh_from_db()
    assert r.delivered_at == first


def test_a_client_supplied_time_is_ignored(team):
    new_task(team)
    r = EventReceipt.objects.get()
    c = login("emp")
    with freeze_time(T0):
        c.post("/app/receipts/ack", {"ids": [str(r.pk)], "delivered_at": "2001-01-01T00:00:00Z"},
               content_type="application/json")  # fmt: skip
    r.refresh_from_db()
    assert r.delivered_at.year == 2026


def test_acking_someone_elses_receipt_is_refused_and_recorded(team):
    new_task(team)
    r = EventReceipt.objects.get()
    resp = ack(login("other"), [r.pk])
    assert resp.status_code == 403
    r.refresh_from_db()
    assert r.delivered_at is None
    assert AuditLog.objects.filter(action="security.denied", actor_id=team["other"].pk).exists()


def test_a_mixed_batch_is_refused_whole(team):
    new_task(team)
    mine = EventReceipt.objects.get()
    new_task(team, who="other")
    theirs = EventReceipt.objects.exclude(pk=mine.pk).get()
    assert ack(login("emp"), [mine.pk, theirs.pk]).status_code == 403
    mine.refresh_from_db()
    assert mine.delivered_at is None


def test_bad_ack_input(team):
    c = login("emp")
    assert ack(c, []).json() == {"acked": 0}
    assert ack(c, [str(uuid.uuid4())]).json() == {"acked": 0}  # nothing by that id: nothing happens
    assert c.post("/app/receipts/ack", {"ids": ["nope"]}, content_type="application/json").status_code == 422
    assert ack(c, [uuid.uuid4() for _ in range(101)]).status_code == 422
    assert Client().post("/app/receipts/ack", {}, content_type="application/json").status_code in (
        302,
        401,
        403,
    )


# --- seen ---------------------------------------------------------------------------------------------------------


def test_opening_the_task_marks_it_seen_and_delivered(team):
    task = new_task(team)
    r = EventReceipt.objects.get()
    with freeze_time(T0):
        assert login("emp").get(f"/app/tasks/{task.pk}").status_code == 200
    r.refresh_from_db()
    assert r.seen_at is not None and r.delivered_at == r.seen_at


def test_other_people_opening_the_task_do_not_mark_it(team):
    task = new_task(team)
    login("boss").get(f"/app/tasks/{task.pk}")
    assert EventReceipt.objects.get().seen_at is None


# --- what the sender sees (F-TK-7) -----------------------------------------------------------------------------------


def marks(viewer, task):
    return receipts.marks_for(viewer, task)


def test_the_four_states_in_order(team):
    with freeze_time(T0):
        task = new_task(team)
        assert marks(team["sub"], task)[team["emp"].pk]["text"] == "Sent"
    with freeze_time("2026-11-16 05:03:00"):  # past the two-minute mark
        assert marks(team["sub"], task)[team["emp"].pk]["text"] == "Not delivered yet"
        ack(login("emp"), [EventReceipt.objects.get().pk])
        assert marks(team["sub"], task)[team["emp"].pk]["text"] == "Delivered"
        login("emp").get(f"/app/tasks/{task.pk}")
        assert marks(team["sub"], task)[team["emp"].pk]["text"] == "Seen"


def test_the_marks_are_on_the_managers_page_and_never_on_the_recipients(team):
    with freeze_time(T0):
        task = new_task(team)
    with freeze_time("2026-11-16 05:05:00"):
        page = login("sub").get(f"/app/tasks/{task.pk}").content.decode()
        assert 'data-receipt="late"' in page and "Not delivered yet" in page
        assert "data-receipt" not in login("emp").get(f"/app/tasks/{task.pk}").content.decode()


def test_an_offline_recipient_catches_up_after_reconnect(team):  # F-TK-7
    with freeze_time(T0):
        new_task(team)
    with freeze_time("2026-11-16 05:10:00"):
        c = login("emp")
        ids = c.get("/app/receipts/pending").json()["ids"]  # what the page fetches first after reconnecting
        assert len(ids) == 1
        ack(c, ids)
        assert c.get("/app/receipts/pending").json()["ids"] == []
    r = EventReceipt.objects.get()
    assert r.delivered_at == datetime.datetime(2026, 11, 16, 5, 10, tzinfo=datetime.UTC)  # the server's time


def test_the_pending_list_is_only_my_own(team):
    new_task(team)
    assert login("other").get("/app/receipts/pending").json() == {"ids": []}


def test_a_delivery_tells_the_senders_page_to_refresh(team, monkeypatch):
    new_task(team)
    r = EventReceipt.objects.get()
    sent = []
    monkeypatch.setattr(publisher, "send", lambda **kw: sent.append(kw))
    ack(login("emp"), [r.pk])
    (m,) = [m for m in sent if m["type_"] == "task-changed"]
    assert str(team["sub"].pk) in m["users"] and m["task_id"] == r.task_id


# --- the manager's list and the urgent notice -------------------------------------------------------------------------


def test_the_dashboard_lists_undelivered_changes_for_the_right_managers(team):
    with freeze_time(T0):
        new_task(team)
        new_task(team, who="other")
    with freeze_time("2026-11-16 05:05:00"):
        assert receipts.undelivered(team["sub"])["count"] == 1  # only their own team
        assert receipts.undelivered(team["boss"])["count"] == 2
        assert receipts.undelivered(team["emp"]) == {"count": 0, "rows": []}
        page = login("sub").get("/app/dashboard").content.decode()
        assert "Not delivered yet (1)" in page
    with freeze_time("2026-11-16 05:00:30"):  # not late yet
        assert receipts.undelivered(team["boss"])["count"] == 0


def test_the_dashboard_list_does_not_grow_its_queries_with_the_data(team):
    with freeze_time(T0):
        new_task(team)
    with freeze_time("2026-11-16 05:05:00"):
        c = login("boss")
        with CaptureQueriesContext(connection) as few:
            c.get("/app/dashboard")
    with freeze_time(T0):
        for _ in range(5):
            new_task(team)
    with freeze_time("2026-11-16 05:05:00"):
        with CaptureQueriesContext(connection) as many:
            c.get("/app/dashboard")
    assert len(many) <= len(few) + 3


def test_an_urgent_change_not_delivered_in_fifteen_minutes_tells_the_sender_once(team):
    with freeze_time(T0):
        new_task(team, Priority.URGENT)
    with freeze_time("2026-11-16 05:14:00"):
        assert receipts.remind_senders() == 0
    with freeze_time("2026-11-16 05:16:00"):
        assert receipts.remind_senders() == 1
        assert receipts.remind_senders() == 0
    (n,) = Notification.objects.filter(kind=Kind.UNDELIVERED)
    assert n.user_id == team["sub"].pk and "Not received yet" in n.title


def test_delivered_or_ordinary_changes_never_trigger_the_notice(team):
    with freeze_time(T0):
        new_task(team, Priority.URGENT)
        new_task(team)  # ordinary
        urgent = EventReceipt.objects.get(urgent=True)
    with freeze_time("2026-11-16 05:05:00"):
        ack(login("emp"), [urgent.pk])
    with freeze_time("2026-11-16 06:00:00"):
        assert receipts.remind_senders() == 0


def test_the_undelivered_job_is_registered_every_minute():
    import ots.notifications.jobs  # noqa: F401, PLC0415
    from ots.core.jobs import registered_jobs

    assert registered_jobs()["notifications.undelivered"].schedule == "* * * * *"


def test_a_receipt_never_changes_the_task(team):
    task = new_task(team)
    before = (task.version, task.derived_status)
    ack(login("emp"), [EventReceipt.objects.get().pk])
    login("emp").get(f"/app/tasks/{task.pk}")
    task.refresh_from_db()
    assert (task.version, task.derived_status) == before
