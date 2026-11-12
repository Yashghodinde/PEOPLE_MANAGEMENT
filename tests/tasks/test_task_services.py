"""M07.3: creating, editing, assigning, reassigning, cancelling and discussing tasks."""

import datetime

import pytest
from ots.accounts.models import Role, User
from ots.core.errors import Conflict, InvalidTransition, PermissionDenied, ValidationFailed
from ots.core.models import OutboxEvent
from ots.tasks import services
from ots.tasks.models import AssignmentStatus as S
from ots.tasks.models import DerivedStatus as D
from ots.tasks.models import Task, TaskAssignment, TaskComment, TaskEvent

from tests.accounts.factories import make_user

pytestmark = pytest.mark.django_db


def due(hours=48):
    return datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=hours)


def new_task(actor, *assignees, **kw):
    kw.setdefault("title", "Prepare the monthly report")
    kw.setdefault("due_at", due())
    return services.create_task(actor, assignee_ids=[u.pk for u in assignees], **kw)


# --- create ----------------------------------------------------------------------------------------------------


def test_creating_a_task_makes_the_assignments_the_history_and_the_events(org):
    task = new_task(org["mgr"], org["emp"], org["peer"], description="Collect the figures", priority="HIGH")
    assert task.task_code.startswith("T-") and task.created_by_id == org["mgr"].pk
    assert (task.derived_status, task.version, task.is_cancelled, task.priority) == (
        D.ASSIGNED,
        1,
        False,
        "HIGH",
    )
    assert sorted(a.user_id for a in task.assignments.all()) == sorted([org["emp"].pk, org["peer"].pk])
    assert all(a.assigned_by_id == org["mgr"].pk and a.status == S.ASSIGNED for a in task.assignments.all())
    kinds = list(TaskEvent.objects.filter(task=task).order_by("id").values_list("event_type", flat=True))
    assert kinds == ["task.created", "assignment.created", "assignment.created"]
    assert OutboxEvent.objects.filter(type="assignment.created").count() == 2
    created = OutboxEvent.objects.filter(type="assignment.created").first()
    assert created.payload["task_code"] == task.task_code and created.payload["manager_id"] == str(
        org["mgr"].pk
    )


def test_each_task_gets_the_next_code():
    sa = make_user(Role.SUPER_ADMIN)
    emp = make_user(Role.EMPLOYEE, reports_to=sa)
    a, b = new_task(sa, emp), new_task(sa, emp)
    assert int(b.task_code[-6:]) == int(a.task_code[-6:]) + 1


@pytest.mark.parametrize(
    "bad,field",
    [
        ({"title": ""}, "title"), ({"title": "   "}, "title"), ({"title": "x" * 201}, "title"),
        ({"priority": "SOON"}, "priority"), ({"due_at": None}, "due_at"),
        ({"due_at": datetime.datetime(2030, 1, 1)}, "due_at"),  # no time zone
        ({"due_at": datetime.datetime(2001, 1, 1, tzinfo=datetime.UTC)}, "due_at"),  # in the past
        ({"category": "x" * 61}, "category"),
    ],
)  # fmt: skip
def test_bad_input_is_refused_with_the_field_named(org, bad, field):
    with pytest.raises(ValidationFailed) as exc:
        new_task(org["mgr"], org["emp"], **bad)
    assert field in exc.value.extra["fields"]
    assert Task.objects.count() == 0  # nothing half-made


def test_the_start_must_come_before_the_due_time(org):
    with pytest.raises(ValidationFailed) as exc:
        new_task(org["mgr"], org["emp"], due_at=due(24), scheduled_start_at=due(30))
    assert "scheduled_start_at" in exc.value.extra["fields"]
    assert (
        new_task(org["mgr"], org["emp"], due_at=due(24), scheduled_start_at=due(2)).scheduled_start_at
        is not None
    )


def test_at_least_one_person_and_no_repeats(org):
    with pytest.raises(ValidationFailed):
        services.create_task(org["mgr"], title="x", due_at=due(), assignee_ids=[])
    task = services.create_task(
        org["mgr"], title="x", due_at=due(), assignee_ids=[org["emp"].pk, org["emp"].pk]
    )
    assert task.assignments.count() == 1


def test_who_may_give_work_to_whom(org):
    assert new_task(org["mgr"], org["emp"], org["mgr"]).assignments.count() == 2  # own team and themselves
    assert (
        new_task(org["sa"], org["outsider"], org["other"]).assignments.count() == 2
    )  # the Super Admin: anyone
    with pytest.raises(PermissionDenied):
        new_task(org["mgr"], org["outsider"])  # another Sub-Admin's employee
    with pytest.raises(PermissionDenied):
        new_task(org["mgr"], org["emp"], org["outsider"])  # one bad name stops the whole task
    assert Task.objects.filter(title="Prepare the monthly report").count() == 2
    for who in ("emp", "peer"):
        with pytest.raises(PermissionDenied):
            new_task(org[who], org["emp"])  # an Employee cannot create tasks


def test_inactive_unknown_and_break_glass_people_cannot_be_given_work(org):
    User.objects.filter(pk=org["peer"].pk).update(is_active=False)
    with pytest.raises(ValidationFailed, match="not active"):
        new_task(org["mgr"], User.objects.get(pk=org["peer"].pk))
    import uuid

    with pytest.raises(ValidationFailed, match="does not exist"):
        services.create_task(org["sa"], title="x", due_at=due(), assignee_ids=[uuid.uuid4()])
    glass = make_user(Role.SUPER_ADMIN, login_id="breakglass", is_break_glass=True)
    with pytest.raises(ValidationFailed):
        new_task(org["sa"], glass)


def test_files_cannot_be_attached_until_the_files_module_exists(org):
    with pytest.raises(ValidationFailed, match="Files module"):
        new_task(org["mgr"], org["emp"], reference_file_ids=["f1"])


# --- update ----------------------------------------------------------------------------------------------------


def test_editing_logs_exactly_what_changed(org):
    task = new_task(org["mgr"], org["emp"])
    updated = services.update_task(
        org["mgr"], task.pk, task.version, title="Prepare the Q3 report", priority="URGENT"
    )
    assert (updated.title, updated.priority, updated.version) == ("Prepare the Q3 report", "URGENT", 2)
    ev = TaskEvent.objects.get(task=task, event_type="task.updated")
    assert ev.payload["changes"] == {
        "title": ["Prepare the monthly report", "Prepare the Q3 report"], "priority": ["NORMAL", "URGENT"],
    }  # fmt: skip
    assert not TaskEvent.objects.filter(task=task, event_type="task.deadline_changed").exists()


def test_a_deadline_change_is_logged_separately_with_old_and_new(org):
    task = new_task(org["mgr"], org["emp"], due_at=due(24))
    new_due = due(72)
    services.update_task(org["mgr"], task.pk, due_at=new_due)
    ev = TaskEvent.objects.get(task=task, event_type="task.deadline_changed")
    assert ev.payload["from"] == task.due_at.isoformat() and ev.payload["to"] == new_due.isoformat()
    with pytest.raises(ValidationFailed):
        services.update_task(org["mgr"], task.pk, due_at=due(-5))  # a new due time must be in the future


def test_an_edit_that_changes_nothing_writes_nothing(org):
    task = new_task(org["mgr"], org["emp"])
    before = TaskEvent.objects.count()
    same = services.update_task(org["mgr"], task.pk, title=task.title, priority=task.priority)
    assert same.version == task.version and TaskEvent.objects.count() == before


def test_editing_checks_the_version_the_person_saw(org):
    task = new_task(org["mgr"], org["emp"])
    services.update_task(org["mgr"], task.pk, task.version, title="Newer title")
    with pytest.raises(Conflict) as exc:
        services.update_task(org["mgr"], task.pk, task.version, title="Stale edit")
    assert exc.value.extra["current"]["version"] == 2 and Task.objects.get(pk=task.pk).title == "Newer title"


def test_unknown_fields_and_cancelled_tasks_cannot_be_edited(org):
    task = new_task(org["mgr"], org["emp"])
    with pytest.raises(ValidationFailed):
        services.update_task(org["mgr"], task.pk, created_by=org["sa"].pk)
    services.cancel_task(org["mgr"], task.pk, "no longer needed")
    with pytest.raises(InvalidTransition):
        services.update_task(org["mgr"], task.pk, title="Too late")


# --- people on the task --------------------------------------------------------------------------------------


def test_adding_people_creates_their_assignments(org):
    task = new_task(org["mgr"], org["emp"])
    (added,) = services.add_assignees(org["mgr"], task.pk, [org["peer"].pk])
    assert added.user_id == org["peer"].pk and added.status == S.ASSIGNED
    assert TaskEvent.objects.filter(task=task, event_type="assignment.created").count() == 2
    with pytest.raises(ValidationFailed, match="already on this task"):
        services.add_assignees(org["mgr"], task.pk, [org["emp"].pk])
    with pytest.raises(PermissionDenied):
        services.add_assignees(org["mgr"], task.pk, [org["outsider"].pk])


def test_a_new_person_reopens_a_finished_task(org):
    task = new_task(org["mgr"], org["emp"])
    TaskAssignment.objects.filter(task=task).update(status=S.APPROVED)
    from ots.tasks.services import recompute_task

    recompute_task(Task.objects.get(pk=task.pk), datetime.datetime.now(datetime.UTC))
    assert Task.objects.get(pk=task.pk).derived_status == D.COMPLETED
    services.add_assignees(org["mgr"], task.pk, [org["peer"].pk])
    reopened = Task.objects.get(pk=task.pk)
    assert reopened.derived_status == D.ASSIGNED and reopened.completed_at is None


def test_a_removed_person_can_be_added_again(org):
    task = new_task(org["mgr"], org["emp"], org["peer"])
    a = task.assignments.get(user=org["emp"])
    services.transition(a.pk, "remove", org["mgr"], a.version, reason="wrong person")
    services.add_assignees(org["mgr"], task.pk, [org["emp"].pk])
    assert task.assignments.filter(user=org["emp"]).count() == 2


def test_reassigning_moves_the_work_in_one_step(org):
    task = new_task(org["mgr"], org["emp"])
    a = task.assignments.get()
    services.transition(a.pk, "start", org["emp"], a.version)
    a.refresh_from_db()
    new = services.reassign(org["mgr"], a.pk, org["peer"].pk, "Asha is on leave", a.version)
    a.refresh_from_db()
    assert (a.status, new.user_id, new.status) == (S.REMOVED, org["peer"].pk, S.ASSIGNED)
    kinds = list(TaskEvent.objects.filter(task=task).order_by("id").values_list("event_type", flat=True))
    assert kinds[-2:] == ["assignment.removed", "assignment.created"]


def test_a_failed_reassignment_changes_nothing(org):
    task = new_task(org["mgr"], org["emp"])
    a = task.assignments.get()
    with pytest.raises(PermissionDenied):
        services.reassign(org["mgr"], a.pk, org["outsider"].pk, "x" * 10)
    a.refresh_from_db()
    assert a.status == S.ASSIGNED and task.assignments.count() == 1  # the old person was NOT removed
    with pytest.raises(ValidationFailed):
        services.reassign(org["mgr"], a.pk, org["emp"].pk, "same person")


# --- cancel ----------------------------------------------------------------------------------------------------


def test_cancelling_stops_unfinished_work_and_keeps_approved_work(org):
    task = new_task(org["mgr"], org["emp"], org["peer"])
    done = task.assignments.get(user=org["emp"])
    TaskAssignment.objects.filter(pk=done.pk).update(status=S.APPROVED)
    cancelled = services.cancel_task(org["mgr"], task.pk, "the client withdrew")
    assert (cancelled.is_cancelled, cancelled.derived_status) == (True, D.CANCELLED)
    statuses = dict(task.assignments.values_list("user__login_id", "status"))
    assert statuses == {"emp": S.APPROVED, "peer": S.CANCELLED}
    assert TaskEvent.objects.filter(task=task, event_type="task.cancelled").count() == 1
    assert (
        TaskEvent.objects.get(task=task, event_type="task.cancelled").payload["reason"]
        == "the client withdrew"
    )
    with pytest.raises(InvalidTransition):
        services.cancel_task(org["mgr"], task.pk, "again please")


def test_cancelling_needs_a_reason_and_the_right_person(org):
    task = new_task(org["mgr"], org["emp"])
    with pytest.raises(ValidationFailed):
        services.cancel_task(org["mgr"], task.pk, "no")
    for who in ("other", "emp"):
        with pytest.raises(PermissionDenied):
            services.cancel_task(org[who], task.pk, "not my task to cancel")
    assert not Task.objects.get(pk=task.pk).is_cancelled
    services.cancel_task(org["sa"], task.pk, "cancelled by the Super Admin")  # any task


def test_work_on_a_cancelled_task_is_refused(org):
    task = new_task(org["mgr"], org["emp"])
    services.cancel_task(org["mgr"], task.pk, "no longer needed")
    a = task.assignments.get()
    with pytest.raises(InvalidTransition):
        services.transition(a.pk, "start", org["emp"], a.version)


# --- comments ----------------------------------------------------------------------------------------------------


def test_comments_are_saved_logged_and_checked(org):
    task = new_task(org["mgr"], org["emp"])
    a = task.assignments.get()
    c = services.add_comment(org["emp"], task.pk, "  Started on the figures.  ", assignment_id=a.pk)
    assert (c.body, c.assignment_id, c.author_id) == ("Started on the figures.", a.pk, org["emp"].pk)
    assert TaskEvent.objects.filter(task=task, event_type="comment.added").count() == 1
    for bad in ("", "   ", "x" * 4001):
        with pytest.raises(ValidationFailed):
            services.add_comment(org["emp"], task.pk, bad)
    other = new_task(org["mgr"], org["peer"]).assignments.get()
    with pytest.raises(ValidationFailed):
        services.add_comment(org["emp"], task.pk, "wrong assignment", assignment_id=other.pk)
    assert TaskComment.objects.filter(task=task).count() == 1


def test_who_may_comment(org):
    task = new_task(org["mgr"], org["emp"])
    services.add_comment(org["sa"], task.pk, "Super Admin may")
    services.add_comment(org["mgr"], task.pk, "The creator may")
    services.add_comment(org["emp"], task.pk, "The assignee may")
    for who in ("peer", "outsider", "other"):
        with pytest.raises(PermissionDenied):
            services.add_comment(org[who], task.pk, "not on this task")
    a = task.assignments.get()
    services.transition(a.pk, "remove", org["mgr"], a.version, reason="moved to another team")
    with pytest.raises(PermissionDenied):
        services.add_comment(
            org["emp"], task.pk, "no longer on it"
        )  # held it once, may still see it, may not post
    services.cancel_task(org["mgr"], task.pk, "no longer needed")
    services.add_comment(org["mgr"], task.pk, "closing remarks are fine on a cancelled task")
