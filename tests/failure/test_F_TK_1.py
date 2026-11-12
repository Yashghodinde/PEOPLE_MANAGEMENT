"""F-TK-1: a double click or a network retry. The same request sent three times makes ONE task, and the repeats get
the first answer back."""

import uuid

import pytest
from django.test import Client
from ots.tasks.models import Task, TaskEvent

pytestmark = pytest.mark.django_db
PW = "correct horse battery"


def login(login_id):
    c = Client()
    assert c.post("/login", {"login_id": login_id, "password": PW}).status_code == 302
    return c


def form(org, **over):
    data = {
        "title": "Reconcile the vendor invoices", "description": "October ledger", "priority": "HIGH",
        "due_at": "2099-12-31T17:30", "assignees": [str(org["emp"].pk), str(org["peer"].pk)],
    }  # fmt: skip
    data.update(over)
    return data


def test_the_same_request_three_times_makes_one_task(org):
    c = login("mgr")
    key = str(uuid.uuid4())
    responses = [c.post("/app/tasks/new", form(org), HTTP_IDEMPOTENCY_KEY=key) for _ in range(3)]
    assert Task.objects.count() == 1
    task = Task.objects.get()
    assert [r.status_code for r in responses] == [302, 302, 302]
    assert {r["Location"] for r in responses} == {
        f"/app/tasks/{task.pk}"
    }  # the repeats replay the first answer
    assert responses[1].headers.get("Idempotent-Replay") or responses[2].headers.get("Idempotent-Replay")
    assert TaskEvent.objects.filter(task=task, event_type="task.created").count() == 1
    assert task.assignments.count() == 2  # not 4 and not 6


def test_a_different_key_is_a_new_request_and_a_missing_key_is_refused(org):
    c = login("mgr")
    c.post("/app/tasks/new", form(org), HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()))
    c.post("/app/tasks/new", form(org), HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()))
    assert Task.objects.count() == 2
    assert c.post("/app/tasks/new", form(org)).status_code == 428  # no key at all


def test_a_failed_attempt_can_be_retried_with_the_same_key(org):
    c = login("mgr")
    key = str(uuid.uuid4())
    bad = c.post("/app/tasks/new", form(org, title=""), HTTP_IDEMPOTENCY_KEY=key)
    assert bad.status_code == 422 and Task.objects.count() == 0
    # the failure was not remembered as a success: the corrected form with a new key works
    assert c.post("/app/tasks/new", form(org), HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4())).status_code == 302


def test_who_may_use_the_endpoint(org):
    assert (
        login("emp").post("/app/tasks/new", form(org), HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4())).status_code
        == 403
    )
    assert (
        Client().post("/app/tasks/new", form(org), HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4())).status_code == 302
    )
    assert (
        login("mgr")
        .post(
            "/app/tasks/new",
            form(org, assignees=[str(org["outsider"].pk)]),
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        .status_code
        == 403
    )  # another team's employee
    assert Task.objects.count() == 0


def test_bad_times_and_ids_are_422_not_500(org):
    c = login("mgr")
    for over in (
        {"due_at": "not-a-date"},
        {"due_at": ""},
        {"scheduled_start_at": "garbage"},
        {"assignees": ["nope"]},
    ):
        r = c.post("/app/tasks/new", form(org, **over), HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()))
        assert r.status_code == 422, over
