"""F-ID-2: the last active (non break-glass) Super Admin cannot be deactivated, demoted or deleted."""

import pytest
from django.db import DatabaseError, connection, transaction
from ots.accounts.models import Role, User

from tests.accounts.factories import make_user

pytestmark = pytest.mark.django_db


def _raw(sql, params):
    with connection.cursor() as cur:
        cur.execute(sql, params)


def test_last_super_admin_cannot_be_deactivated_or_demoted():
    sa = make_user(Role.SUPER_ADMIN)
    make_user(Role.SUPER_ADMIN, is_break_glass=True)  # break-glass does not count
    with pytest.raises(DatabaseError, match="last active Super Admin"), transaction.atomic():
        _raw("UPDATE users SET is_active = false WHERE id = %s", [sa.id])
    with pytest.raises(DatabaseError, match="last active Super Admin"), transaction.atomic():
        _raw("UPDATE users SET role = 'SUB_ADMIN', reports_to_id = %s WHERE id = %s", [sa.id, sa.id])


def test_second_super_admin_allows_demoting_the_first():
    sa1 = make_user(Role.SUPER_ADMIN)
    sa2 = make_user(Role.SUPER_ADMIN)
    _raw("UPDATE users SET role = 'SUB_ADMIN', reports_to_id = %s WHERE id = %s", [sa2.id, sa1.id])
    assert User.objects.get(id=sa1.id).role == Role.SUB_ADMIN
    # now sa2 is the last one
    with pytest.raises(DatabaseError, match="last active Super Admin"), transaction.atomic():
        _raw("UPDATE users SET is_active = false WHERE id = %s", [sa2.id])


def test_users_cannot_be_deleted():
    sa = make_user(Role.SUPER_ADMIN)
    emp = make_user(Role.EMPLOYEE, reports_to=sa)
    with pytest.raises(DatabaseError, match="never deleted"), transaction.atomic():
        _raw("DELETE FROM users WHERE id = %s", [emp.id])
