"""M07.5: the task screens. Who sees what, the forms without JavaScript, the action buttons, query counts."""

import uuid

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from ots.tasks.models import AssignmentStatus, Task

from tests.accounts.factories import make_user  # noqa: F401
from tests.tasks.factories import make_task

pytestmark = pytest.mark.django_db
PW = "correct horse battery"


def login(login_id):
    c = Client()
    assert c.post("/login", {"login_id": login_id, "password": PW}).status_code == 302
    return c


def test_task_list_is_scoped_by_role(org, task):
    stranger = make_task(org["other"], org["outsider"], title="Secret plans")
    assert task.task_code in login("emp").get("/app/tasks").content.decode()
    assert stranger.task_code not in login("emp").get("/app/tasks").content.decode()
    assert stranger.task_code not in login("mgr").get("/app/tasks").content.decode()
    body = login("boss").get("/app/tasks").content.decode()
    assert task.task_code in body and stranger.task_code in body


def test_only_managers_get_an_add_task_button(org):
    assert "Add task" in login("mgr").get("/app/tasks").content.decode()
    assert "Add task" not in login("emp").get("/app/tasks").content.decode()
    assert login("emp").get("/app/tasks/new").status_code == 403


def test_list_filters_and_bad_input_never_500(org, task):
    c = login("boss")
    for qs in (
        "status=ASSIGNED",
        "status=zzz",
        "q=%00",
        "priority=URGENT",
        "overdue=1",
        "mine=1",
        "page=abc",
    ):
        assert c.get(f"/app/tasks?{qs}").status_code == 200
    assert task.task_code in c.get("/app/tasks?q=monthly").content.decode()
    assert task.task_code not in c.get("/app/tasks?q=nothing-like-this").content.decode()


def test_list_query_count_is_flat(org):
    for i in range(50):
        make_task(org["mgr"], org["emp"], org["peer"], title=f"Task {i}")
    c = login("mgr")
    with CaptureQueriesContext(connection) as q:
        assert c.get("/app/tasks").status_code == 200
    assert len(q) <= 12  # session + user + count + page + prefetch, not 50 more


def test_detail_hides_tasks_from_people_who_cannot_see_them(org, task):
    assert login("emp").get(f"/app/tasks/{task.pk}").status_code == 200
    assert login("outsider").get(f"/app/tasks/{task.pk}").status_code == 404
    assert login("emp").get(f"/app/tasks/{uuid.uuid4()}").status_code == 404
    assert login("emp").get("/app/tasks/not-a-uuid").status_code == 404


def test_create_form_works_without_javascript_and_keeps_input_on_error(org):
    c = login("mgr")
    form = {
        "title": "",
        "priority": "NORMAL",
        "due_at": "2099-01-01T10:00",
        "assignees": [str(org["emp"].pk)],
    }
    bad = c.post("/app/tasks/new", form)
    assert bad.status_code == 422 and "<html" in bad.content.decode()
    assert 'value="2099-01-01T10:00"' in bad.content.decode()
    form["title"] = "Quarterly audit"
    ok = c.post("/app/tasks/new", form)
    task = Task.objects.get(title="Quarterly audit")
    assert ok.status_code == 302 and ok["Location"] == f"/app/tasks/{task.pk}"


def test_assignee_sees_friendly_wording_and_can_start_and_finish(org, task, assignment):
    c = login("emp")
    body = c.get(f"/app/tasks/{task.pk}").content.decode()
    assert "Not started" in body and f"/app/assignments/{assignment.pk}/start" in body
    assert f"/app/assignments/{assignment.pk}/approve" not in body  # an assignee never reviews their own work
    r = c.post(f"/app/assignments/{assignment.pk}/start", {"version": assignment.version})
    assert r.status_code == 302
    assignment.refresh_from_db()
    assert assignment.status == AssignmentStatus.IN_PROGRESS
    assert "Working" in c.get(f"/app/tasks/{task.pk}").content.decode()


def test_a_short_closing_note_is_a_form_error_on_the_same_page(org, task, assignment):
    c = login("emp")
    c.post(f"/app/assignments/{assignment.pk}/start", {"version": assignment.version})
    assignment.refresh_from_db()
    r = c.post(f"/app/assignments/{assignment.pk}/submit", {"version": assignment.version, "note": "hi"})
    assert r.status_code == 422 and "At least 10 characters" in r.content.decode()
    assignment.refresh_from_db()
    assert assignment.status == AssignmentStatus.IN_PROGRESS


def test_a_stale_or_missing_version_changes_nothing(org, task, assignment):
    c = login("emp")
    assert c.post(f"/app/assignments/{assignment.pk}/start", {}).status_code == 422
    assert (
        c.post(f"/app/assignments/{assignment.pk}/start", {"version": assignment.version + 5}).status_code
        == 409
    )
    assignment.refresh_from_db()
    assert assignment.status == AssignmentStatus.ASSIGNED


def test_outsiders_and_unknown_actions_get_404(org, task, assignment):
    assert (
        login("outsider").post(f"/app/assignments/{assignment.pk}/start", {"version": 1}).status_code == 404
    )
    assert login("emp").post(f"/app/assignments/{assignment.pk}/pause_eod", {"version": 1}).status_code == 404
    assert login("emp").post(f"/app/assignments/{assignment.pk}/nonsense", {"version": 1}).status_code == 404


def test_comment_cancel_and_add_people(org, task):
    c = login("mgr")
    assert c.post(f"/app/tasks/{task.pk}/comment", {"body": "Please hurry"}).status_code == 302
    assert "Please hurry" in c.get(f"/app/tasks/{task.pk}").content.decode()
    task.refresh_from_db()
    assert (
        c.post(
            f"/app/tasks/{task.pk}/people", {"assignees": [str(org["peer"].pk)], "version": task.version}
        ).status_code
        == 302
    )
    assert task.assignments.count() == 2
    task.refresh_from_db()
    assert (
        c.post(
            f"/app/tasks/{task.pk}/cancel", {"reason": "no longer needed", "version": task.version}
        ).status_code
        == 302
    )
    task.refresh_from_db()
    assert task.is_cancelled
    assert (
        login("emp")
        .post(f"/app/tasks/{task.pk}/cancel", {"reason": "nope nope", "version": task.version})
        .status_code
        == 403
    )


def test_dashboards_per_role_and_query_budget(org, task):
    for i in range(20):
        make_task(org["mgr"], org["emp"], title=f"More {i}")
    for who in ("boss", "mgr"):
        c = login(who)
        with CaptureQueriesContext(connection) as q:
            body = c.get("/app/dashboard").content.decode()
        # 14 for the tasks, plus the team-today widget (D-062): flat, whatever the team size
        assert len(q) <= 24 and "Overdue" in body
    c = login("emp")
    with CaptureQueriesContext(connection) as q:
        body = c.get("/app/dashboard").content.decode()
    assert len(q) <= 12 and "Not started" in body


def test_manager_dashboard_is_scoped_to_their_team(org, task):
    other = make_task(org["other"], org["outsider"], title="Elsewhere work")
    assert other.task_code not in login("mgr").get("/app/dashboard").content.decode()
    assert "Ravi Patel" not in login("mgr").get("/app/dashboard").content.decode()
    assert "Ravi Patel" in login("boss").get("/app/dashboard").content.decode()
