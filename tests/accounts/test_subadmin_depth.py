"""IMP-13.0 (D-095): a Sub-Admin may report to a Sub-Admin; nothing else about rank changed, and loops are refused
in the service and by the database trigger."""

import pytest
from django.db import IntegrityError, connection, transaction
from ots.accounts import people, services
from ots.accounts.models import Role, User
from ots.core.errors import ValidationFailed

from tests.accounts.factories import make_user

pytestmark = pytest.mark.django_db


@pytest.fixture
def tree():
    sa = make_user(Role.SUPER_ADMIN, login_id="boss")
    director = make_user(Role.SUB_ADMIN, reports_to=sa, login_id="dir")
    manager = make_user(Role.SUB_ADMIN, reports_to=director, login_id="mgr")
    emp = make_user(Role.EMPLOYEE, reports_to=manager, login_id="e1")
    return sa, director, manager, emp


def test_a_sub_admin_can_sit_under_a_sub_admin_four_levels_deep(tree):
    sa, director, manager, emp = tree
    sub_manager = make_user(Role.SUB_ADMIN, reports_to=manager, login_id="sm")
    deep = make_user(Role.EMPLOYEE, reports_to=sub_manager, login_id="e2")
    assert deep.reports_to_id == sub_manager.pk and sub_manager.reports_to_id == manager.pk


def test_the_manager_choices_for_a_sub_admin_include_other_sub_admins(tree):
    sa, director, manager, emp = tree
    names = {u.pk for u in people._manager_choices(Role.SUB_ADMIN)}
    assert {sa.pk, director.pk, manager.pk} <= names


def test_an_employee_is_never_a_manager(tree):
    sa, director, manager, emp = tree
    with pytest.raises(ValidationFailed):
        services.set_manager(sa, make_user(Role.EMPLOYEE, reports_to=manager, login_id="e3"), emp)


def test_a_loop_is_refused_by_the_service(tree):
    sa, director, manager, emp = tree
    with pytest.raises(ValidationFailed, match="loop"):
        services.set_manager(sa, director, manager)


def test_a_loop_is_refused_by_the_database_even_if_the_service_is_skipped(tree):
    sa, director, manager, emp = tree
    with pytest.raises(IntegrityError), transaction.atomic():
        User.objects.filter(pk=director.pk).update(reports_to=manager)
    with pytest.raises(IntegrityError), transaction.atomic():
        User.objects.filter(pk=director.pk).update(reports_to=director)
    assert connection.in_atomic_block


def test_a_sub_admin_cannot_be_put_under_an_employee(tree):
    sa, director, manager, emp = tree
    with pytest.raises(ValidationFailed):
        services.set_manager(sa, manager, emp)
