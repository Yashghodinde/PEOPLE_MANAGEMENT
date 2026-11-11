"""F-HR-1: loops, inactive managers and lower-rank managers are rejected by the DB (raw SQL, no services)."""

import pytest
from django.db import DatabaseError, IntegrityError, connection, transaction
from ots.accounts.models import Role

from tests.accounts.factories import make_user

pytestmark = pytest.mark.django_db


def _raw(sql, params):
    with connection.cursor() as cur:
        cur.execute(sql, params)


@pytest.fixture
def tree():
    sa = make_user(Role.SUPER_ADMIN)
    sub = make_user(Role.SUB_ADMIN, reports_to=sa)
    emp = make_user(Role.EMPLOYEE, reports_to=sub)
    return sa, sub, emp


def test_reports_to_required_for_non_super_admin():
    with pytest.raises(IntegrityError), transaction.atomic():
        from ots.accounts.models import User

        from tests.accounts.factories import company

        u = User(
            company=company(), role=Role.EMPLOYEE, login_id="orphan", employee_id="E-orphan", full_name="O"
        )
        u.set_password("correct horse battery")
        u.save()


def test_self_report_and_cycle_rejected_with_all_triggers(tree):
    sa, sub, emp = tree
    with pytest.raises(DatabaseError), transaction.atomic():
        _raw("UPDATE users SET reports_to_id = id WHERE id = %s", [emp.id])
    with pytest.raises(DatabaseError), transaction.atomic():
        _raw("UPDATE users SET reports_to_id = %s WHERE id = %s", [emp.id, sa.id])


def test_cycle_trigger_alone_catches_loops(tree):
    """Rank rules already make loops impossible; prove the cycle trigger is an independent backstop."""
    sa, sub, emp = tree
    _raw("SET CONSTRAINTS ALL IMMEDIATE", [])
    _raw("ALTER TABLE users DISABLE TRIGGER users_manager_valid", [])  # rolled back with the test
    with pytest.raises(DatabaseError, match="users_no_cycle: a user cannot report"), transaction.atomic():
        _raw("UPDATE users SET reports_to_id = id WHERE id = %s", [emp.id])
    with pytest.raises(DatabaseError, match="users_no_cycle: reporting loop"), transaction.atomic():
        _raw("UPDATE users SET reports_to_id = %s WHERE id = %s", [emp.id, sub.id])  # sub -> emp -> sub
    with pytest.raises(DatabaseError, match="users_no_cycle: reporting loop"), transaction.atomic():
        _raw("UPDATE users SET reports_to_id = %s WHERE id = %s", [emp.id, sa.id])  # sa -> emp -> sub -> sa


def test_inactive_manager_rejected(tree):
    sa, _sub, _emp = tree
    sub2 = make_user(Role.SUB_ADMIN, reports_to=sa)
    _raw("UPDATE users SET is_active = false WHERE id = %s", [sub2.id])
    with pytest.raises(DatabaseError, match="not active"), transaction.atomic():
        make_user(Role.EMPLOYEE, reports_to=sub2)


def test_lower_or_equal_rank_manager_rejected(tree):
    sa, sub, emp = tree
    with pytest.raises(DatabaseError, match="higher rank"), transaction.atomic():
        make_user(Role.SUB_ADMIN, reports_to=emp)
    with pytest.raises(DatabaseError, match="higher rank"), transaction.atomic():
        make_user(Role.EMPLOYEE, reports_to=make_user(Role.EMPLOYEE, reports_to=sub))


def test_manager_with_active_reports_cannot_be_deactivated_or_demoted(tree):
    _sa, sub, _emp = tree
    with pytest.raises(DatabaseError, match="active reports"), transaction.atomic():
        _raw("UPDATE users SET is_active = false WHERE id = %s", [sub.id])
    with pytest.raises(DatabaseError, match="active reports"), transaction.atomic():
        _raw("UPDATE users SET role = 'EMPLOYEE' WHERE id = %s", [sub.id])
