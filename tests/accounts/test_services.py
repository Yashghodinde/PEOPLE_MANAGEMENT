import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from ots.accounts import selectors, services
from ots.accounts.models import ReportingHistory, Role, User
from ots.audit.models import AuditLog
from ots.core.errors import Conflict, PermissionDenied, ValidationFailed
from ots.core.models import OutboxEvent

from tests.accounts.factories import make_user

pytestmark = pytest.mark.django_db
PW = "a-long-enough-password"


@pytest.fixture
def sa():
    return make_user(Role.SUPER_ADMIN)


@pytest.fixture
def sub(sa):
    return make_user(Role.SUB_ADMIN, reports_to=sa)


def _new(actor, role, mgr, **kw):
    n = User.objects.count()
    return services.create_user(
        actor, login_id=f"p{n}", employee_id=f"E{n}", full_name=f"P {n}", role=role,
        reports_to=mgr, password=PW, **kw,
    )  # fmt: skip


def test_create_user_writes_history_audit_event(sa, sub):
    u = _new(sa, Role.EMPLOYEE, sub, designation="Clerk")
    assert u.reports_to == sub and u.must_change_password and u.designation == "Clerk"
    h = ReportingHistory.objects.get(user=u)
    assert h.manager == sub and h.effective_to is None
    assert AuditLog.objects.filter(action="user.created", target_id=str(u.pk), actor_id=sa.pk).count() == 1
    assert OutboxEvent.objects.filter(type="user.created", aggregate_id=str(u.pk)).count() == 1


def test_only_super_admin_may_write(sa, sub):
    emp = _new(sa, Role.EMPLOYEE, sub)
    for actor in (sub, emp):
        with pytest.raises(PermissionDenied):
            _new(actor, Role.EMPLOYEE, sub)
        with pytest.raises(PermissionDenied):
            services.update_profile(actor, emp, designation="x")
        with pytest.raises(PermissionDenied):
            services.set_manager(actor, emp, sa)
        with pytest.raises(PermissionDenied):
            services.deactivate_user(actor, emp)
        with pytest.raises(PermissionDenied):
            services.reset_password(actor, emp, PW + "x")


def test_rank_rules(sa, sub):
    emp = _new(sa, Role.EMPLOYEE, sub)
    _new(sa, Role.EMPLOYEE, sa)  # employee directly under the super admin is allowed
    with pytest.raises(ValidationFailed):
        _new(sa, Role.EMPLOYEE, emp)  # equal rank
    with pytest.raises(ValidationFailed):
        _new(sa, Role.SUB_ADMIN, emp)  # lower rank
    with pytest.raises(ValidationFailed):
        _new(sa, Role.EMPLOYEE, None)  # manager required
    with pytest.raises(ValidationFailed):
        _new(sa, Role.SUPER_ADMIN, sa)  # a super admin has no manager


def test_duplicate_login_is_friendly_error(sa, sub):
    u = _new(sa, Role.EMPLOYEE, sub)
    with pytest.raises(ValidationFailed, match="login ID"):
        services.create_user(
            sa, login_id=u.login_id.upper(), employee_id="Z1", full_name="Z", role=Role.EMPLOYEE,
            reports_to=sub, password=PW,
        )  # fmt: skip


def test_password_policy(sa, sub):
    with pytest.raises(ValidationFailed):
        services.create_user(
            sa, login_id="short", employee_id="Z2", full_name="Z", role=Role.EMPLOYEE,
            reports_to=sub, password="short",
        )  # fmt: skip
    with pytest.raises(ValidationFailed):
        services.create_user(
            sa, login_id="samesamesame", employee_id="Z3", full_name="Z", role=Role.EMPLOYEE,
            reports_to=sub, password="samesamesame",
        )  # fmt: skip


def test_set_manager_loop_and_noop(sa, sub):
    sub2 = make_user(Role.SUB_ADMIN, reports_to=sa)
    emp = _new(sa, Role.EMPLOYEE, sub)
    with pytest.raises(ValidationFailed):
        services.set_manager(sa, sub, emp)  # lower rank
    before = AuditLog.objects.count()
    services.set_manager(sa, emp, sub)  # unchanged: no audit, no history
    assert AuditLog.objects.count() == before
    services.set_manager(sa, emp, sub2)
    assert User.objects.get(pk=emp.pk).reports_to == sub2


def test_deactivate_blocked_while_reports_exist_and_lists_names(sa, sub):
    emp = _new(sa, Role.EMPLOYEE, sub)
    with pytest.raises(ValidationFailed) as e:
        services.deactivate_user(sa, sub)
    assert emp.full_name in e.value.message
    services.set_manager(sa, emp, sa)
    services.deactivate_user(sa, sub)
    assert not User.objects.get(pk=sub.pk).is_active
    assert AuditLog.objects.filter(action="user.deactivated").exists()


def test_cannot_deactivate_self_or_last_super_admin(sa):
    with pytest.raises(ValidationFailed):
        services.deactivate_user(sa, sa)
    sa2 = make_user(Role.SUPER_ADMIN)
    services.deactivate_user(sa, sa2)  # fine, sa remains
    sa3 = make_user(Role.SUPER_ADMIN)
    services.deactivate_user(sa3, sa)  # fine, sa3 remains
    with pytest.raises(ValidationFailed):
        services.deactivate_user(sa3, sa3)


def test_optimistic_lock(sa, sub):
    emp = _new(sa, Role.EMPLOYEE, sub)
    services.update_profile(sa, emp, expected_version=emp.version, designation="A")
    with pytest.raises(Conflict):
        services.update_profile(sa, emp, expected_version=emp.version, designation="B")


def test_demotion_blocked_while_reports(sa, sub):
    _new(sa, Role.EMPLOYEE, sub)
    with pytest.raises(ValidationFailed):
        services.update_profile(sa, sub, role=Role.EMPLOYEE)


def test_reset_and_change_password(sa, sub):
    emp = _new(sa, Role.EMPLOYEE, sub)
    emp = services.reset_password(sa, emp, "brand-new-pass-1")
    assert emp.must_change_password and emp.check_password("brand-new-pass-1")
    with pytest.raises(ValidationFailed):
        services.change_password(emp, "wrong", "another-long-pass-2")
    emp = services.change_password(emp, "brand-new-pass-1", "another-long-pass-2")
    assert not emp.must_change_password
    assert AuditLog.objects.filter(action__in=["user.password_reset", "user.password_changed"]).count() == 2
    with pytest.raises(PermissionDenied):
        services.reset_password(emp, sub, "zzzzzzzzzzzz")


def test_subtree_ids_single_query(sa, sub):
    emp = _new(sa, Role.EMPLOYEE, sub)
    with CaptureQueriesContext(connection) as q:
        ids = selectors.subtree_ids(sa.pk)
    assert len(q) == 1
    assert ids == {sub.pk, emp.pk}
    assert selectors.subtree_ids(emp.pk) == set()


def test_failed_service_leaves_no_audit_or_event(sa, sub):
    before = (AuditLog.objects.count(), OutboxEvent.objects.count(), User.objects.count())
    with pytest.raises(ValidationFailed):
        _new(sa, Role.EMPLOYEE, None)
    assert (AuditLog.objects.count(), OutboxEvent.objects.count(), User.objects.count()) == before


def test_audit_record_requires_transaction():
    # pytest-django wraps tests in a transaction, so simulate "outside" by checking the guard directly
    from unittest import mock

    import ots.audit.api as audit

    with mock.patch("ots.audit.api.connection") as c:
        c.in_atomic_block = False
        with pytest.raises(RuntimeError):
            audit.record("x.y")
