"""F-TK-5: a deactivated assignee. Work cannot be given to an inactive person, and a person with open work cannot be
deactivated until it is reassigned: the refusal lists exactly what is open."""

import pytest
from ots.accounts import services as people
from ots.core.errors import ValidationFailed
from ots.tasks import services
from ots.tasks.models import AssignmentStatus as S
from ots.tasks.models import TaskAssignment

from tests.tasks.factories import make_task

pytestmark = pytest.mark.django_db


def test_deactivation_is_refused_while_work_is_open_and_the_refusal_lists_it(org):
    t1 = make_task(org["mgr"], org["emp"], title="Month-end closing")
    t2 = make_task(org["mgr"], org["emp"], title="Update the vendor list")
    with pytest.raises(ValidationFailed) as exc:
        people.deactivate_user(org["sa"], org["emp"])
    assert "Month-end closing" in exc.value.message and "Update the vendor list" in exc.value.message
    assert sorted(exc.value.extra["fields"]["assignments"]) == sorted(
        [f"{t1.task_code} Month-end closing", f"{t2.task_code} Update the vendor list"]
    )
    org["emp"].refresh_from_db()
    assert org["emp"].is_active  # nothing happened


@pytest.mark.parametrize("status", [S.ASSIGNED, S.IN_PROGRESS, S.PAUSED, S.BLOCKED, S.CHANGES_REQUESTED])
def test_every_kind_of_open_work_blocks_deactivation(org, status):
    task = make_task(org["mgr"], org["emp"])
    TaskAssignment.objects.filter(task=task).update(status=status)
    with pytest.raises(ValidationFailed, match="open work"):
        people.deactivate_user(org["sa"], org["emp"])


@pytest.mark.parametrize("status", [S.SUBMITTED, S.APPROVED, S.REMOVED, S.CANCELLED])
def test_work_that_is_finished_or_waiting_for_someone_else_does_not_block(org, status):
    task = make_task(org["mgr"], org["emp"])
    TaskAssignment.objects.filter(task=task).update(status=status)
    assert people.deactivate_user(org["sa"], org["emp"]).is_active is False


def test_work_on_a_cancelled_task_does_not_block(org):
    task = make_task(org["mgr"], org["emp"])
    services.cancel_task(org["mgr"], task.pk, "the client withdrew")
    assert people.deactivate_user(org["sa"], org["emp"]).is_active is False


def test_after_reassigning_the_person_can_be_deactivated(org):
    task = make_task(org["mgr"], org["emp"])
    a = task.assignments.get()
    with pytest.raises(ValidationFailed):
        people.deactivate_user(org["sa"], org["emp"])
    services.reassign(org["mgr"], a.pk, org["peer"].pk, "Asha is leaving the company", a.version)
    assert people.deactivate_user(org["sa"], org["emp"]).is_active is False


def test_work_cannot_be_given_to_an_inactive_person_in_any_way(org):
    task = make_task(org["mgr"], org["emp"])
    people.deactivate_user(org["sa"], org["peer"])  # peer has nothing open
    from ots.accounts.models import User

    peer = User.objects.get(pk=org["peer"].pk)
    a = task.assignments.get()
    import datetime

    due = datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=2)
    with pytest.raises(ValidationFailed, match="not active"):
        services.create_task(org["mgr"], title="New", due_at=due, assignee_ids=[peer.pk])
    with pytest.raises(ValidationFailed, match="not active"):
        services.add_assignees(org["mgr"], task.pk, [peer.pk])
    with pytest.raises(ValidationFailed, match="not active"):
        services.reassign(org["mgr"], a.pk, peer.pk, "to someone who left")
    a.refresh_from_db()
    assert a.status == S.ASSIGNED and task.assignments.count() == 1  # the failed reassignment removed nobody


def test_a_person_reactivated_can_be_given_work_again(org):
    people.deactivate_user(org["sa"], org["peer"])
    people.reactivate_user(org["sa"], org["peer"].__class__.objects.get(pk=org["peer"].pk))
    task = make_task(org["mgr"], org["emp"])
    from ots.accounts.models import User

    services.add_assignees(org["mgr"], task.pk, [User.objects.get(pk=org["peer"].pk).pk])
    assert task.assignments.count() == 2
