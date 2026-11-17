"""Notifications, the bell count and the dots (live push and ticks arrive with M10)."""

import pytest
from django.test import Client
from ots.notifications.models import Kind, Notification
from ots.tasks import services

from tests.tasks.factories import make_task

pytestmark = pytest.mark.django_db
PW = "correct horse battery"


def login(login_id):
    c = Client()
    assert c.post("/login", {"login_id": login_id, "password": PW}).status_code == 302
    return c


def kinds(user):
    return list(Notification.objects.filter(user=user).values_list("kind", flat=True))


def test_a_message_notifies_the_other_people_but_not_the_writer(org, task):
    services.add_comment(org["mgr"], task.pk, "Please send the figures by noon")
    assert kinds(org["emp"]) == [Kind.COMMENT] and kinds(org["mgr"]) == []
    services.add_comment(org["emp"], task.pk, "Sending now")
    assert kinds(org["mgr"]) == [Kind.COMMENT]
    n = Notification.objects.get(user=org["emp"])
    assert n.body.startswith("Please send") and org["mgr"].full_name in n.title


def test_people_outside_the_task_hear_nothing(org, task):
    services.add_comment(org["mgr"], task.pk, "Private to this task")
    for who in ("outsider", "peer", "other"):
        assert kinds(org[who]) == []


def test_assigned_submitted_changes_approved_and_completed_reach_the_right_person(org):
    t = services.create_task(
        org["mgr"],
        title="Audit",
        due_at=make_task(org["mgr"], org["emp"]).due_at,
        assignee_ids=[org["emp"].pk],
    )
    a = t.assignments.get()
    assert kinds(org["emp"]) == [Kind.ASSIGNED] and kinds(org["mgr"]) == []
    services.transition(a.pk, "start", org["emp"])
    a.refresh_from_db()
    services.transition(a.pk, "submit", org["emp"], note="All done, files attached.")
    assert kinds(org["mgr"]) == [Kind.SUBMITTED]
    a.refresh_from_db()
    services.transition(a.pk, "request_changes", org["mgr"], feedback="Add totals please")
    assert Kind.CHANGES_REQUESTED in kinds(org["emp"])
    services.transition(a.pk, "resume", org["emp"])
    services.transition(a.pk, "submit", org["emp"], note="Totals added now.")
    services.transition(a.pk, "approve", org["mgr"])
    assert Kind.APPROVED in kinds(org["emp"])
    assert (
        Notification.objects.filter(user=org["mgr"], kind=Kind.COMPLETED).count() == 0
    )  # the approver did it


def test_completion_tells_the_task_creator_when_someone_else_approves(org):
    t = services.create_task(
        org["mgr"],
        title="Audit",
        due_at=make_task(org["mgr"], org["emp"]).due_at,
        assignee_ids=[org["emp"].pk],
    )
    a = t.assignments.get()
    services.transition(a.pk, "start", org["emp"])
    services.transition(a.pk, "submit", org["emp"], note="All done, files attached.")
    services.transition(a.pk, "approve", org["sa"])
    assert Kind.COMPLETED in kinds(org["mgr"])


def test_bell_count_dots_and_clearing(org, task):
    services.add_comment(org["mgr"], task.pk, "Look at this")
    emp = login("emp")
    assert '<span class="ots-count">1</span>' in emp.get("/app/tasks/all").content.decode()
    assert "ots-dot-chat" in emp.get("/app/tasks/all").content.decode()
    assert "ots-dot-chat" in emp.get("/app/dashboard").content.decode()
    assert "Look at this" in emp.get("/app/notifications").content.decode()
    emp.get(f"/app/tasks/{task.pk}")  # opening the task clears it
    body = emp.get("/app/tasks/all").content.decode()
    assert "ots-count" not in body and "ots-dot-chat" not in body


def test_finished_work_shows_a_green_dot_and_a_message_wins_over_it(org, task):
    a = task.assignments.get(user=org["emp"])
    services.transition(a.pk, "start", org["emp"])
    services.transition(a.pk, "submit", org["emp"], note="All done, files attached.")
    services.transition(a.pk, "approve", org["mgr"])
    assert "ots-dot-done" in login("emp").get("/app/tasks/all").content.decode()
    services.add_comment(org["mgr"], task.pk, "Well done")
    body = login("emp").get("/app/tasks/all").content.decode()
    assert "ots-dot-chat" in body and "ots-dot-done" not in body


def test_the_notification_page_opens_the_task_and_only_for_its_owner(org, task):
    services.add_comment(org["mgr"], task.pk, "Hello")
    n = Notification.objects.get(user=org["emp"])
    assert (
        login("peer").post(f"/app/notifications/{n.pk}/open").status_code == 302
    )  # not theirs: back to the list
    n.refresh_from_db()
    assert n.read_at is None
    r = login("emp").post(f"/app/notifications/{n.pk}/open")
    assert r["Location"] == f"/app/tasks/{task.pk}"
    n.refresh_from_db()
    assert n.read_at is not None
    assert login("emp").post("/app/notifications/not-a-uuid/open").status_code == 302


def test_mark_all_read_and_empty_bell(org, task):
    services.add_comment(org["mgr"], task.pk, "One")
    services.add_comment(org["mgr"], task.pk, "Two")
    emp = login("emp")
    assert emp.post("/app/notifications/read-all").status_code == 302
    assert not Notification.objects.filter(user=org["emp"], read_at__isnull=True).exists()
    assert "Nothing new" in login("outsider").get("/app/notifications").content.decode()


def test_a_removed_person_is_told_and_a_cancelled_task_tells_its_people(org, task):
    services.cancel_task(org["mgr"], task.pk, "no longer needed")
    assert Kind.CANCELLED in kinds(org["emp"])
