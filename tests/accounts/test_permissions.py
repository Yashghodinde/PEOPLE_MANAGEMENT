import itertools

import pytest
from ots.accounts.models import Role, User
from ots.core import permissions

from tests.accounts.factories import make_user

pytestmark = pytest.mark.django_db

ADMIN_ONLY = ["user.create", "user.update", "user.deactivate", "user.set_manager", "user.reset_password"]
ROLES = [Role.SUPER_ADMIN, Role.SUB_ADMIN, Role.EMPLOYEE]


@pytest.fixture
def org():
    sa = make_user(Role.SUPER_ADMIN)
    sub = make_user(Role.SUB_ADMIN, reports_to=sa)
    other_sub = make_user(Role.SUB_ADMIN, reports_to=sa)
    emp = make_user(Role.EMPLOYEE, reports_to=sub)
    other_emp = make_user(Role.EMPLOYEE, reports_to=other_sub)
    return {"sa": sa, "sub": sub, "other_sub": other_sub, "emp": emp, "other_emp": other_emp}


def _viewer(org, role):
    return {Role.SUPER_ADMIN: org["sa"], Role.SUB_ADMIN: org["sub"], Role.EMPLOYEE: org["emp"]}[role]


@pytest.mark.parametrize(("role", "action"), list(itertools.product(ROLES, ADMIN_ONLY)))
def test_admin_actions_matrix(org, role, action):
    assert permissions.can(_viewer(org, role), action, org["emp"]) is (role == Role.SUPER_ADMIN)


@pytest.mark.parametrize(
    ("role", "target", "expected"),
    [
        (Role.SUPER_ADMIN, "sa", True), (Role.SUPER_ADMIN, "other_emp", True),
        (Role.SUB_ADMIN, "sub", True), (Role.SUB_ADMIN, "emp", True),
        (Role.SUB_ADMIN, "other_emp", False), (Role.SUB_ADMIN, "other_sub", False), (Role.SUB_ADMIN, "sa", False),
        (Role.EMPLOYEE, "emp", True), (Role.EMPLOYEE, "other_emp", False), (Role.EMPLOYEE, "sub", False),
    ],
)  # fmt: skip
def test_user_view_matrix(org, role, target, expected):
    assert permissions.can(_viewer(org, role), "user.view", org[target]) is expected


@pytest.mark.parametrize(
    ("role", "expected"), [(Role.SUPER_ADMIN, True), (Role.SUB_ADMIN, True), (Role.EMPLOYEE, False)]
)
def test_org_chart_matrix(org, role, expected):
    assert permissions.can(_viewer(org, role), "org.view_chart") is expected


@pytest.mark.parametrize("role", ROLES)
def test_audit_view_allowed_for_all_roles_scope_applied_in_viewer(org, role):
    assert permissions.can(_viewer(org, role), "audit.view") is True


@pytest.mark.parametrize("role", ROLES)
def test_change_password_only_for_self(org, role):
    me = _viewer(org, role)
    assert permissions.can(me, "user.change_password", me)
    assert not permissions.can(me, "user.change_password", org["other_emp"])


def test_inactive_and_anonymous_are_denied_everywhere(org):
    ghost = make_user(Role.EMPLOYEE, reports_to=org["sa"], is_active=False)
    for action in [*ADMIN_ONLY, "user.view", "org.view_chart", "audit.view"]:
        assert not permissions.can(ghost, action, ghost)
        assert not permissions.can(None, action, ghost)


def test_visible_to_scopes_rows(org):
    names = lambda u: {x.pk for x in User.objects.visible_to(u)}  # noqa: E731
    assert names(org["sa"]) == {u.pk for u in org.values()}
    assert names(org["sub"]) == {org["sub"].pk, org["emp"].pk}
    assert names(org["emp"]) == {org["emp"].pk}
    assert names(None) == set()
    assert names(make_user(Role.EMPLOYEE, reports_to=org["sa"], is_active=False)) == set()
