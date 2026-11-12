"""M07.3: the permission matrix for tasks, and which tasks each person can see."""

import pytest
from ots.accounts.models import Role
from ots.core.permissions import can
from ots.tasks.models import AssignmentStatus as S
from ots.tasks.models import Task, TaskAssignment

from tests.accounts.factories import make_user
from tests.tasks.factories import make_task

pytestmark = pytest.mark.django_db


@pytest.fixture
def world(org):
    """One task by `mgr` for `emp`; a second assignee `peer`; plus a removed person and people unrelated to it."""
    task = make_task(org["mgr"], org["emp"], org["peer"])
    gone = make_user(Role.EMPLOYEE, reports_to=org["mgr"], login_id="gone")
    TaskAssignment.objects.create(task=task, user=gone, assigned_by=org["mgr"], status=S.REMOVED)
    TaskAssignment.objects.filter(user=gone).update(removed_at=task.created_at)
    watcher = make_user(Role.SUB_ADMIN, reports_to=org["sa"], login_id="watcher")  # unrelated to the task
    return {**org, "gone": gone, "watcher": watcher, "the_task": task}


# (action, who) -> allowed? Everyone not listed is refused.
MATRIX = {
    "task.create": {"sa", "mgr", "other", "watcher"},
    "task.update": {"sa", "mgr"},
    "task.cancel": {"sa", "mgr"},
    "assignment.add": {"sa", "mgr"},
    "task.view": {"sa", "mgr", "emp", "peer", "gone"},
    "task.comment": {"sa", "mgr", "emp", "peer"},
}
PEOPLE = ["sa", "mgr", "other", "watcher", "emp", "peer", "gone", "outsider"]


@pytest.mark.parametrize("action", sorted(MATRIX))
@pytest.mark.parametrize("who", PEOPLE)
def test_task_permission_matrix(world, action, who):
    obj = None if action == "task.create" else world["the_task"]
    expected = who in MATRIX[action]
    if action == "task.view" and who == "other":
        expected = False  # `other` has no assignee on this task
    assert can(world[who], action, obj) is expected, f"{who} / {action}"


def test_a_sub_admin_also_sees_a_task_when_an_assignee_is_in_their_team(world):
    boss_task = make_task(world["sa"], world["emp"])  # created by the Super Admin, assigned to mgr's employee
    assert can(world["mgr"], "task.view", boss_task) and can(world["mgr"], "task.comment", boss_task)
    assert not can(world["mgr"], "task.update", boss_task)  # can see it, but it is not theirs to edit
    assert not can(world["other"], "task.view", boss_task)


def test_a_sub_admin_sees_a_task_assigned_to_themselves(world):
    mine = make_task(world["sa"], world["other"])
    assert can(world["other"], "task.view", mine) and can(world["other"], "task.comment", mine)


def test_anonymous_and_inactive_users_can_do_nothing(world):
    from django.contrib.auth.models import AnonymousUser
    from ots.accounts.models import User

    for action in MATRIX:
        assert can(AnonymousUser(), action, world["the_task"]) is False
    User.objects.filter(pk=world["peer"].pk).update(is_active=False)  # a leaf person (no reports)
    peer = User.objects.get(pk=world["peer"].pk)
    assert (
        can(peer, "task.view", world["the_task"]) is False
        and can(peer, "task.comment", world["the_task"]) is False
    )


# --- the scoped queryset -----------------------------------------------------------------------------------------


def ids(user):
    return set(Task.objects.visible_to(user).values_list("pk", flat=True))


def test_each_role_sees_only_its_tasks(world):
    mine = world["the_task"]
    other_team = make_task(world["other"], world["outsider"])
    by_boss = make_task(world["sa"], world["emp"])  # an assignee in mgr's team
    assert ids(world["sa"]) == {mine.pk, other_team.pk, by_boss.pk}
    assert ids(world["mgr"]) == {mine.pk, by_boss.pk}  # created, or an assignee in the team
    assert ids(world["other"]) == {other_team.pk}
    assert ids(world["watcher"]) == set()
    assert ids(world["emp"]) == {mine.pk, by_boss.pk}
    assert ids(world["peer"]) == {mine.pk}
    assert ids(world["gone"]) == {mine.pk}  # held it once
    assert ids(world["outsider"]) == {other_team.pk}


def test_a_task_with_two_assignees_in_the_team_appears_once(world):
    assert Task.objects.visible_to(world["mgr"]).filter(pk=world["the_task"].pk).count() == 1


def test_anonymous_and_deactivated_people_see_nothing(world):
    from django.contrib.auth.models import AnonymousUser
    from ots.accounts.models import User

    assert (
        not Task.objects.visible_to(AnonymousUser()).exists() and not Task.objects.visible_to(None).exists()
    )
    User.objects.filter(pk=world["emp"].pk).update(is_active=False)
    assert not Task.objects.visible_to(User.objects.get(pk=world["emp"].pk)).exists()
