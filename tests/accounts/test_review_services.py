"""Regression tests for the M02 code-review findings on services, triggers and reporting history."""

import datetime

import pytest
from django.db import DatabaseError, connection, transaction
from django.test.utils import CaptureQueriesContext
from freezegun import freeze_time
from ots.accounts import services
from ots.accounts.models import ReportingHistory, Role, User
from ots.core.errors import Conflict, ValidationFailed

from tests.accounts.factories import make_user

pytestmark = pytest.mark.django_db
PW = "a-long-enough-password"


@pytest.fixture
def sa():
    return make_user(Role.SUPER_ADMIN)


@pytest.fixture
def sa2():
    return make_user(Role.SUPER_ADMIN)


@pytest.fixture
def sub(sa):
    return make_user(Role.SUB_ADMIN, reports_to=sa)


@pytest.fixture
def emp(sub):
    return make_user(Role.EMPLOYEE, reports_to=sub)


# --- S1: a Super Admin can be demoted (role and manager change in one save) ---------------------


def test_super_admin_can_be_demoted_with_a_manager(sa, sa2):
    cur = services.update_profile(sa, sa2, role=Role.SUB_ADMIN, reports_to=sa, expected_version=sa2.version)
    assert cur.role == Role.SUB_ADMIN and cur.reports_to_id == sa.pk
    assert ReportingHistory.objects.filter(user=sa2, manager=sa, effective_to__isnull=True).count() == 1


def test_demotion_without_a_manager_is_refused_with_a_clear_message(sa, sa2):
    with pytest.raises(ValidationFailed, match="reports to"):
        services.update_profile(sa, sa2, role=Role.SUB_ADMIN)
    assert User.objects.get(pk=sa2.pk).role == Role.SUPER_ADMIN


def test_last_super_admin_still_cannot_be_demoted(sa, sub):
    with pytest.raises(ValidationFailed, match="last active Super Admin"):
        services.update_profile(sa, sa, role=Role.SUB_ADMIN, reports_to=sub)


def test_promotion_takes_the_higher_manager_in_one_save(sa, sub, emp):
    with pytest.raises(ValidationFailed):  # manager is now below the new rank
        services.update_profile(sa, emp, role=Role.SUB_ADMIN)
    cur = services.update_profile(sa, emp, role=Role.SUB_ADMIN, reports_to=sa)
    assert cur.role == Role.SUB_ADMIN and cur.reports_to_id == sa.pk


# --- S2: creating under a manager uses the manager's real, locked state --------------------------


def test_create_under_a_manager_deactivated_a_moment_ago_is_refused(sa, sub):
    stale = User.objects.get(pk=sub.pk)  # the form loaded this while the manager was still active
    User.objects.filter(pk=sub.pk).update(is_active=False)
    with pytest.raises(ValidationFailed, match="not active"):
        services.create_user(
            sa, login_id="late", employee_id="E-late", full_name="Late", role=Role.EMPLOYEE,
            reports_to=stale, password=PW,
        )  # fmt: skip
    assert not User.objects.filter(login_id="late").exists()


# --- S3: reactivation re-checks the manager, in the service and in the database ------------------


def test_reactivate_under_an_inactive_manager_is_refused(sa, sub, emp):
    services.deactivate_user(sa, emp)
    services.deactivate_user(sa, sub)
    with pytest.raises(ValidationFailed, match="manager is inactive"):
        services.reactivate_user(sa, User.objects.get(pk=emp.pk))


def test_database_blocks_reactivating_under_an_inactive_manager(sa, sub, emp):
    services.deactivate_user(sa, emp)
    services.deactivate_user(sa, sub)
    with pytest.raises(DatabaseError, match="manager is not active"), transaction.atomic():
        with connection.cursor() as cur:
            cur.execute("UPDATE users SET is_active = true WHERE id = %s", [emp.pk])


# --- S4: the person and the manager are locked in one ordered statement -------------------------


def test_set_manager_takes_both_locks_in_one_ordered_query(sa, sub, emp):
    other = make_user(Role.SUB_ADMIN, reports_to=sa)
    with CaptureQueriesContext(connection) as ctx:
        services.set_manager(sa, emp, other)
    locks = [
        q["sql"] for q in ctx.captured_queries if "FOR UPDATE" in q["sql"] and 'FROM "users"' in q["sql"]
    ]
    assert len(locks) == 1 and 'ORDER BY "users"."id"' in locks[0] and "IN (" in locks[0]


# --- S5: a database refusal becomes a friendly error, not a 500 ---------------------------------


def test_database_refusal_is_reported_as_validation_error(monkeypatch, sa, sub, emp):
    monkeypatch.setattr(services, "_check_manager", lambda *a, **k: None)  # skip the friendly check
    lower = make_user(Role.EMPLOYEE, reports_to=sub)
    with pytest.raises(ValidationFailed, match="hierarchy rule"):
        services.set_manager(sa, emp, lower)  # an employee cannot manage: the trigger says no


def test_duplicate_employee_id_race_is_a_field_error(sa, sub, monkeypatch):
    first = make_user(Role.EMPLOYEE, reports_to=sub, employee_id="E-same")
    original = services.User.objects.filter

    def blind_precheck(*args, **kwargs):  # the pre-check "misses" the other admin's row, as in a real race
        if kwargs.get("employee_id") == "E-same":
            return User.objects.none()
        return original(*args, **kwargs)

    monkeypatch.setattr(services.User.objects, "filter", blind_precheck)
    with pytest.raises(ValidationFailed) as exc:
        services.create_user(
            sa, login_id="dup", employee_id="E-same", full_name="Dup", role=Role.EMPLOYEE,
            reports_to=sub, password=PW,
        )  # fmt: skip
    assert "employee_id" in exc.value.extra["fields"] and first.pk


# --- S6: every update path can carry a version -------------------------------------------------


def test_reactivate_and_reset_refuse_a_stale_version(sa, emp):
    services.deactivate_user(sa, emp)
    stale = User.objects.get(pk=emp.pk).version - 1
    with pytest.raises(Conflict):
        services.reactivate_user(sa, emp, expected_version=stale)
    with pytest.raises(Conflict):
        services.reset_password(sa, emp, "temporary-pass-9", expected_version=stale)


# --- S7: same-day manager changes keep history valid -------------------------------------------


def test_two_changes_on_one_day_leave_no_empty_rows(sa, sub, emp):
    other = make_user(Role.SUB_ADMIN, reports_to=sa)
    with freeze_time("2026-11-12 06:00:00"):
        services.set_manager(sa, emp, other)
        services.set_manager(sa, User.objects.get(pk=emp.pk), sub)
    rows = list(ReportingHistory.objects.filter(user=emp).order_by("effective_from"))
    assert all(r.effective_to is None or r.effective_to > r.effective_from for r in rows)
    current = [r for r in rows if r.effective_to is None]
    assert len(current) == 1 and current[0].manager_id == sub.pk
    assert rows[0].effective_to == datetime.date(2026, 11, 12)  # the original row from 2000 closed today


def test_database_rejects_an_empty_history_interval(emp):
    with pytest.raises(DatabaseError), transaction.atomic():
        ReportingHistory.objects.create(
            user=emp, manager=emp.reports_to, effective_from=datetime.date(2026, 1, 1),
            effective_to=datetime.date(2026, 1, 1),
        )  # fmt: skip


# --- P6: the break-glass account is changed only from the server shell --------------------------


def test_break_glass_cannot_be_edited_moved_or_deactivated_by_an_admin(sa):
    bg = make_user(Role.SUPER_ADMIN, login_id="breakglass", is_break_glass=True)
    with pytest.raises(ValidationFailed, match="server shell"):
        services.update_profile(sa, bg, designation="x")
    with pytest.raises(ValidationFailed, match="server shell"):
        services.deactivate_user(sa, bg)
    sub = make_user(Role.SUB_ADMIN, reports_to=sa)
    with pytest.raises(ValidationFailed, match="server shell"):
        services.set_manager(sa, bg, None)
    services.reset_password(sa, bg, "rotated-sealed-pass-1")  # rotating the sealed password stays allowed
    with pytest.raises(ValidationFailed, match="break-glass"):
        services.set_manager(sa, sub, bg)  # and it never becomes anyone's manager
