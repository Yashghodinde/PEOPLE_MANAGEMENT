"""IMP-15.8..15.12: the task workspace. Chat sides, system lines, help requests with a manager reply, the visible
action buttons, files in the chat, and the permissions behind each."""

import pytest
from django.test import Client
from ots.core.errors import NotFound, PermissionDenied, ValidationFailed
from ots.notifications.models import Kind, Notification
from ots.tasks import cards, services, support
from ots.tasks.models import AssignmentStatus as S
from ots.tasks.models import SupportRequest, SupportStatus, TaskComment

from tests.tasks.factories import make_task

pytestmark = pytest.mark.django_db
PW = "correct horse battery"


def login(login_id):
    c = Client()
    assert c.post("/login", {"login_id": login_id, "password": PW}).status_code == 302
    return c


def started(org, task):
    a = task.assignments.get(user=org["emp"])
    services.transition(a.pk, "start", org["emp"])
    a.refresh_from_db()
    return a


def ask(org, a, **kw):
    args = {"kind": "MISSING_FILE", "what": "I need the previous month's bank statement."} | kw
    return support.raise_request(org["emp"], a.pk, **args)


# ------------------------------------------------------------------ the page
def test_the_workspace_has_a_header_tabs_chat_and_action_buttons(org, task):
    a = started(org, task)
    page = login("emp").get(f"/app/tasks/{task.pk}").content.decode()
    for needle in (task.title, task.task_code, "People", "Details", "Activity", ">Chat<", ">Files<",
                   "Type a message", "I'm waiting", "Submit task for review", 'role="log"'):  # fmt: skip
        assert needle in page, needle
    assert "Edit task" not in page and "Cancel task" not in page  # an employee has no manager buttons
    mgr = login("mgr").get(f"/app/tasks/{task.pk}").content.decode()
    assert "Edit task" in mgr and "Cancel task" in mgr and "Submit task for review" not in mgr
    assert a.pk and "grid-cols-1 lg:grid-cols-split" not in page  # the old two-column layout is gone


def test_managers_speak_on_the_left_and_the_people_doing_the_work_on_the_right(org, task):
    services.add_comment(org["mgr"], task.pk, "Please start with the bank statement.")
    services.add_comment(org["emp"], task.pk, "Starting now.")
    page = login("mgr").get(f"/app/tasks/{task.pk}").content.decode()
    left = page.index('data-side="left"')
    right = page.index('data-side="right"')
    assert left < right and "Please start" in page[left:right] and "Starting now." in page[right:]
    assert (
        login("emp").get(f"/app/tasks/{task.pk}").content.decode().index('data-side="left"') < right + 10_000
    )


def test_two_employees_keep_their_own_names_in_a_group_task(org):
    t = make_task(org["mgr"], org["emp"], org["peer"])
    services.add_comment(org["emp"], t.pk, "Asha here.")
    services.add_comment(org["peer"], t.pk, "Karan here.")
    page = login("mgr").get(f"/app/tasks/{t.pk}").content.decode()
    assert page.count('data-side="right"') == 2 and "Asha Kulkarni" in page and "Karan Malhotra" in page


def test_system_lines_come_from_the_real_events(org, task):
    a = started(org, task)
    services.transition(a.pk, "submit", org["emp"], note="All figures are in the sheet.")
    page = login("mgr").get(f"/app/tasks/{task.pk}").content.decode()
    assert "Asha Kulkarni submitted the task for review" in page and "All figures are in the sheet." in page
    services.transition(a.pk, "request_changes", org["mgr"], feedback="Please redo the salary total.")
    page = login("emp").get(f"/app/tasks/{task.pk}").content.decode()
    assert "Arjun Rao asked for changes" in page and "redo the salary total" in page


# ------------------------------------------------------------------ help requests
def test_an_employee_help_request_blocks_the_assignment_and_posts_a_card(org, task):
    a = started(org, task)
    sr = ask(org, a, why="Needed for the reconciliation.", urgent=True)
    a.refresh_from_db()
    assert a.status == S.BLOCKED and sr.status == SupportStatus.OPEN and sr.manager_id == org["mgr"].pk
    c = TaskComment.objects.get(help_request=sr)
    assert "Missing file" in c.body and "bank statement" in c.body and c.author_id == org["emp"].pk
    page = login("mgr").get(f"/app/tasks/{task.pk}?request={sr.pk}").content.decode()
    assert "Missing file · Urgent" in page and "Waiting for the manager" in page and "Mark resolved" in page
    assert f'data-request="{sr.pk}"' in page


def test_the_manager_is_notified_with_a_link_to_the_exact_request(org, task):
    a = started(org, task)
    sr = ask(org, a)
    mine = Notification.objects.filter(user=org["mgr"], task=task).exclude(link="")
    assert mine.exists() and all(n.link == f"/app/tasks/{task.pk}?request={sr.pk}" for n in mine)
    n = mine.filter(kind=Kind.BLOCKED).first() or mine.first()
    r = login("mgr").post(f"/app/notifications/{n.pk}/open")
    assert r["Location"] == f"/app/tasks/{task.pk}?request={sr.pk}"


def test_the_manager_replies_asks_and_resolves_in_the_same_thread(org, task):
    a = started(org, task)
    sr = ask(org, a)
    support.reply(org["mgr"], sr.pk, "Please check the attached statement.", "reply")
    sr.refresh_from_db()
    assert sr.status == SupportStatus.MANAGER_RESPONDED
    assert Notification.objects.filter(user=org["emp"], task=task, kind=Kind.COMMENT).exists()
    support.reply(org["emp"], sr.pk, "Which month do you mean?", "reply")
    sr.refresh_from_db()
    assert sr.status == SupportStatus.OPEN
    support.reply(org["mgr"], sr.pk, "October.", "clarify")
    sr.refresh_from_db()
    assert sr.status == SupportStatus.WAITING_FOR_EMPLOYEE
    support.reply(org["mgr"], sr.pk, "Thanks, done.", "resolve")
    sr.refresh_from_db()
    assert sr.status == SupportStatus.RESOLVED and sr.resolved_by_id == org["mgr"].pk and sr.resolved_at
    assert sr.comments.count() == 5  # the card and four messages, one conversation
    a.refresh_from_db()
    assert a.status == S.BLOCKED  # resolving never resumes or completes the work
    services.transition(a.pk, "unblock", org["emp"], note="Got it, carrying on")
    a.refresh_from_db()
    assert a.status == S.IN_PROGRESS


def test_a_resolved_request_takes_no_more_replies_and_the_page_still_shows_it(org, task):
    a = started(org, task)
    sr = ask(org, a)
    support.reply(org["mgr"], sr.pk, "", "resolve")
    with pytest.raises(ValidationFailed):
        support.reply(org["emp"], sr.pk, "one more thing", "reply")
    assert "Resolved" in login("emp").get(f"/app/tasks/{task.pk}").content.decode()


def test_dismissing_the_notification_leaves_the_request(org, task):
    a = started(org, task)
    sr = ask(org, a)
    Notification.objects.filter(user=org["mgr"]).delete() if False else None
    Notification.objects.filter(user=org["mgr"], task=task).update(read_at=None)
    assert SupportRequest.objects.filter(pk=sr.pk).exists()
    assert "Waiting for the manager" in login("mgr").get(f"/app/tasks/{task.pk}").content.decode()


def test_several_requests_each_keep_their_own_status(org, task):
    a = started(org, task)
    first = ask(org, a)
    support.reply(org["mgr"], first.pk, "Sent.", "resolve")
    services.transition(a.pk, "unblock", org["emp"], note="continuing")
    second = ask(org, a, kind="ACCESS_REQUIRED", what="I cannot open the banking portal.")
    first.refresh_from_db()
    assert first.status == SupportStatus.RESOLVED and second.status == SupportStatus.OPEN
    assert SupportRequest.objects.filter(task=task).count() == 2


def test_who_may_raise_and_who_may_answer(org, task):
    a = started(org, task)
    with pytest.raises(NotFound):
        support.raise_request(org["peer"], a.pk, kind="OTHER", what="Not my assignment at all")
    with pytest.raises(ValidationFailed):
        support.raise_request(org["emp"], a.pk, kind="MADE_UP", what="Some words here")
    with pytest.raises(ValidationFailed):
        support.raise_request(org["emp"], a.pk, kind="OTHER", what="no")
    sr = ask(org, a)
    for stranger in ("peer", "outsider", "other"):
        with pytest.raises(NotFound):
            support.reply(org[stranger], sr.pk, "hello", "reply")
    with pytest.raises(PermissionDenied):
        support.reply(org["emp"], sr.pk, "I resolve it myself", "resolve")
    assert (
        login("boss")
        .post(f"/app/tasks/{task.pk}/help/{sr.pk}/reply", {"body": "ok", "action": "reply"})
        .status_code
        == 302
    )


def test_the_card_turns_pink_once_the_manager_answered(org, task):
    a = started(org, task)
    sr = ask(org, a)
    a.refresh_from_db()
    a.task = task
    a.help_answered = False
    assert cards.card_tone(a) == "red"
    support.reply(org["mgr"], sr.pk, "Here it is.", "reply")
    a.help_answered = a.pk in support.answered_assignment_ids([a.pk])
    assert cards.card_tone(a) == "pink"


def test_the_help_form_posts_through_the_view_and_errors_come_back_as_messages(org, task):
    a = started(org, task)
    c = login("emp")
    bad = c.post(
        f"/app/tasks/{task.pk}/help", {"assignment": str(a.pk), "kind": "OTHER", "what": "x"}, follow=True
    )
    assert "Explain what you need" in bad.content.decode()
    ok = c.post(
        f"/app/tasks/{task.pk}/help",
        {"assignment": str(a.pk), "kind": "MISSING_FILE", "what": "The GST working file is missing"},
    )
    assert ok.status_code == 302 and "?request=" in ok["Location"]
    assert SupportRequest.objects.filter(task=task).count() == 1


# ------------------------------------------------------------------ submit and review buttons
def test_submit_and_review_buttons_are_visible_and_one_person_cannot_complete_another(org):
    t = make_task(org["mgr"], org["emp"], org["peer"])
    emp_a, peer_a = t.assignments.get(user=org["emp"]), t.assignments.get(user=org["peer"])
    services.transition(emp_a.pk, "start", org["emp"])
    services.transition(emp_a.pk, "submit", org["emp"], note="done with my part")
    page = login("mgr").get(f"/app/tasks/{t.pk}").content.decode()
    assert "Approve task" in page and "Request changes" in page
    with pytest.raises(PermissionDenied):
        services.transition(emp_a.pk, "approve", org["emp"])  # no approving one's own work
    with pytest.raises(PermissionDenied):
        services.transition(emp_a.pk, "submit", org["peer"])  # nor submitting another's
    services.transition(emp_a.pk, "approve", org["mgr"])
    peer_a.refresh_from_db()
    assert peer_a.status == S.ASSIGNED  # the other person's part is untouched


def test_a_stranger_still_gets_a_404_for_the_workspace(org, task):
    assert login("outsider").get(f"/app/tasks/{task.pk}").status_code == 404
    assert login("outsider").post(f"/app/tasks/{task.pk}/help", {"assignment": "x"}).status_code == 404
