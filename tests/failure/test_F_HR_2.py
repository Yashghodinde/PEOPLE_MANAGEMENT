"""F-HR-2: a manager change keeps history. The person is still in the old manager's subtree for past dates."""

import datetime

import pytest
from freezegun import freeze_time
from ots.accounts import selectors, services
from ots.accounts.models import ReportingHistory, Role
from ots.audit.models import AuditLog

from tests.accounts.factories import make_user

pytestmark = pytest.mark.django_db
PW = "a-long-enough-password"


def test_manager_change_keeps_past_subtree():
    sa = make_user(Role.SUPER_ADMIN)
    old = make_user(Role.SUB_ADMIN, reports_to=sa)
    new = make_user(Role.SUB_ADMIN, reports_to=sa)
    with freeze_time("2026-11-01 06:00:00"):  # 11:30 IST
        emp = services.create_user(
            sa, login_id="e1", employee_id="E1", full_name="E One", role=Role.EMPLOYEE,
            reports_to=old, password=PW,
        )  # fmt: skip
    day = datetime.date(2026, 11, 10)
    with freeze_time("2026-11-10 06:00:00"):
        services.set_manager(sa, emp, new)

    assert selectors.subtree_ids(old.pk) == set()
    assert selectors.subtree_ids(new.pk) == {emp.pk}
    # the day before the change the person still counted under the old manager
    assert emp.pk in selectors.subtree_ids_at(old.pk, day - datetime.timedelta(days=1))
    assert emp.pk not in selectors.subtree_ids_at(new.pk, day - datetime.timedelta(days=1))
    # from the change day on, the new manager has them
    assert emp.pk in selectors.subtree_ids_at(new.pk, day)
    assert emp.pk not in selectors.subtree_ids_at(old.pk, day)
    # a manager above both sees them throughout
    assert emp.pk in selectors.subtree_ids_at(sa.pk, day - datetime.timedelta(days=1))

    rows = list(ReportingHistory.objects.filter(user=emp).order_by("effective_from"))
    assert [r.manager_id for r in rows] == [old.pk, new.pk]
    assert rows[0].effective_to == day and rows[1].effective_to is None
    assert AuditLog.objects.filter(action="user.manager_changed", target_id=str(emp.pk)).count() == 1
