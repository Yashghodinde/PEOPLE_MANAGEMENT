"""IMP-08.1: the people-management truth table (role x action x target role) and the HR role's database rules."""

import itertools

import pytest
from django.db import IntegrityError, transaction
from ots.accounts import services
from ots.accounts.models import Role, User
from ots.accounts.permissions import MANAGE_ACTIONS, NewPerson, can_assign_role, can_manage_user
from ots.core import permissions
from ots.core.errors import PermissionDenied, ValidationFailed

from tests.accounts.factories import make_user

pytestmark = pytest.mark.django_db
PW = "a-long-enough-password"
ROLES = [Role.SUPER_ADMIN, Role.HR, Role.SUB_ADMIN, Role.EMPLOYEE]

# The truth table from the plan, written out. {actor role: {action: set of target roles it may be done to}}
# "create" is about the role being created (a Sub-Admin only for an Employee reporting to them).
ALLOWED = {
    Role.SUPER_ADMIN: {a: set(ROLES) for a in MANAGE_ACTIONS},
    Role.HR: {a: {Role.SUB_ADMIN, Role.EMPLOYEE} for a in MANAGE_ACTIONS},
    Role.SUB_ADMIN: {a: ({Role.EMPLOYEE} if a == "create" else set()) for a in MANAGE_ACTIONS},
    Role.EMPLOYEE: {a: set() for a in MANAGE_ACTIONS},
}


@pytest.fixture
def world():
    sa = make_user(Role.SUPER_ADMIN, login_id="sa")
    hr = make_user(Role.HR, reports_to=sa, login_id="hr")
    sub = make_user(Role.SUB_ADMIN, reports_to=sa, login_id="sub")
    emp = make_user(Role.EMPLOYEE, reports_to=sub, login_id="emp")
    return {Role.SUPER_ADMIN: sa, Role.HR: hr, Role.SUB_ADMIN: sub, Role.EMPLOYEE: emp}


def test_the_whole_matrix_matches_the_table(world):
    for actor_role, action, target_role in itertools.product(ROLES, MANAGE_ACTIONS, ROLES):
        actor = world[actor_role]
        if action == "create":
            target = NewPerson(target_role, manager=actor)  # a Sub-Admin creates under themselves
        else:
            target = world[target_role]
        expected = target_role in ALLOWED[actor_role][action]
        assert can_manage_user(actor, action, target) is expected, (actor_role, action, target_role)
        assert permissions.can(actor, f"user.{action}", target) is expected


def test_a_sub_admin_creates_only_inside_their_own_team(world):
    sub, sa = world[Role.SUB_ADMIN], world[Role.SUPER_ADMIN]
    assert can_manage_user(sub, "create", NewPerson(Role.EMPLOYEE, manager=sub))
    assert not can_manage_user(sub, "create", NewPerson(Role.EMPLOYEE, manager=sa))
    assert not can_manage_user(sub, "create", NewPerson(Role.EMPLOYEE, manager=None))


def test_screens_open_for_the_roles_that_can_do_something(world):
    for action in MANAGE_ACTIONS:
        assert {r for r in ROLES if can_manage_user(world[r], action)} == (
            {Role.SUPER_ADMIN, Role.HR, Role.SUB_ADMIN} if action == "create" else {Role.SUPER_ADMIN, Role.HR}
        )


def test_which_roles_each_actor_may_hand_out(world):
    got = {r: {x for x in ROLES if can_assign_role(world[r], x)} for r in ROLES}
    assert got == {
        Role.SUPER_ADMIN: set(ROLES),
        Role.HR: {Role.SUB_ADMIN, Role.EMPLOYEE},
        Role.SUB_ADMIN: {Role.EMPLOYEE},
        Role.EMPLOYEE: set(),
    }


def test_inactive_and_unknown_actors_can_do_nothing(world):
    hr = world[Role.HR]
    User.objects.filter(pk=hr.pk).update(is_active=False)
    hr.refresh_from_db()
    assert not can_manage_user(hr, "create", NewPerson(Role.EMPLOYEE, manager=world[Role.SUB_ADMIN]))
    assert not can_manage_user(None, "update", world[Role.EMPLOYEE])
    assert not can_manage_user(world[Role.SUPER_ADMIN], "delete", world[Role.EMPLOYEE])  # not an action


# --- the services follow the same table ---------------------------------------------------------------------


def _create(actor, role, manager, n):
    return services.create_user(
        actor, login_id=f"n{n}", employee_id=f"N{n}", full_name=f"New {n}", role=role, reports_to=manager,
        password=PW,
    )  # fmt: skip


def test_hr_creates_sub_admins_and_employees_but_never_hr_or_super_admin(world):
    sa, hr, sub = world[Role.SUPER_ADMIN], world[Role.HR], world[Role.SUB_ADMIN]
    assert _create(hr, Role.SUB_ADMIN, sa, 1).role == Role.SUB_ADMIN
    assert _create(hr, Role.EMPLOYEE, sub, 2).role == Role.EMPLOYEE
    with pytest.raises(PermissionDenied):
        _create(hr, Role.HR, sa, 3)
    with pytest.raises(PermissionDenied):
        _create(hr, Role.SUPER_ADMIN, None, 4)


def test_hr_edits_people_but_not_super_admins_or_other_hr_and_cannot_promote(world):
    sa, hr, sub, emp = (world[r] for r in ROLES)
    services.update_profile(hr, emp, designation="Clerk")
    assert User.objects.get(pk=emp.pk).designation == "Clerk"
    other_hr = make_user(Role.HR, reports_to=sa, login_id="hr2")
    for target in (sa, other_hr):
        with pytest.raises(PermissionDenied):
            services.update_profile(hr, target, designation="x")
        with pytest.raises(PermissionDenied):
            services.deactivate_user(hr, target)
        with pytest.raises(PermissionDenied):
            services.reset_password(hr, target, PW + "x")
    with pytest.raises(PermissionDenied):  # editing a person's role up to HR is creating an HR account
        services.update_profile(hr, emp, role=Role.HR, reports_to=sa)
    services.set_manager(hr, emp, sa)
    services.deactivate_user(hr, emp)
    assert not User.objects.get(pk=emp.pk).is_active


def test_a_sub_admin_cannot_edit_anyone_or_create_above_employee(world):
    sa, hr, sub, emp = (world[r] for r in ROLES)
    for fn in (
        lambda: services.update_profile(sub, emp, designation="x"),
        lambda: services.set_manager(sub, emp, sub),
        lambda: services.deactivate_user(sub, emp),
        lambda: services.reset_password(sub, emp, PW + "x"),
        lambda: _create(sub, Role.SUB_ADMIN, sa, 5),
        lambda: _create(sub, Role.HR, sa, 6),
        lambda: _create(sub, Role.EMPLOYEE, sa, 7),  # an Employee, but not in their own team
    ):
        with pytest.raises(PermissionDenied):
            fn()


# --- the database backs the rules up --------------------------------------------------------------------------


def test_the_database_accepts_hr_under_a_super_admin_only(world):
    sa, sub = world[Role.SUPER_ADMIN], world[Role.SUB_ADMIN]
    assert make_user(Role.HR, reports_to=sa).role == Role.HR
    with pytest.raises(IntegrityError), transaction.atomic():
        make_user(Role.HR, reports_to=sub)  # a Sub-Admin cannot manage an HR account


def test_an_hr_account_cannot_be_anyones_manager(world):
    hr = world[Role.HR]
    with pytest.raises(IntegrityError), transaction.atomic():
        make_user(Role.EMPLOYEE, reports_to=hr)
    with pytest.raises(ValidationFailed):  # and the service says so in plain words first
        _create(world[Role.SUPER_ADMIN], Role.EMPLOYEE, hr, 8)


def test_a_manager_cannot_be_turned_into_hr_while_they_still_have_reports(world):
    sa, sub = world[Role.SUPER_ADMIN], world[Role.SUB_ADMIN]
    with pytest.raises(IntegrityError), transaction.atomic():
        User.objects.filter(pk=sub.pk).update(
            role=Role.HR
        )  # the employee under them would have an HR manager
    assert User.objects.get(pk=sa.pk).role == Role.SUPER_ADMIN


def test_people_lists_show_hr_everyone_but_the_break_glass_account(world):
    make_user(Role.SUPER_ADMIN, login_id="breakglass", is_break_glass=True)
    seen = set(User.objects.visible_to(world[Role.HR]).values_list("login_id", flat=True))
    assert seen == {"sa", "hr", "sub", "emp"}
