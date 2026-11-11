"""Two real database connections race: the manager lock must stop the hierarchy ending up inconsistent."""

import threading

import pytest
from django.db import connection, transaction
from ots.accounts import services
from ots.accounts.models import Company, Role, User
from ots.core.errors import ValidationFailed

from tests.accounts.factories import make_user


@pytest.fixture
def keep_default_company():
    """transaction=True tests flush the database; put back the company the data migration created."""
    yield
    Company.objects.get_or_create(code="DEFAULT", defaults={"name": "Default company"})


def _race(first, second):
    """Run `first` inside an open transaction, then start `second` while `first` has not yet committed."""
    first_done, second_started = threading.Event(), threading.Event()
    result: dict[str, object] = {}

    def run_first():
        try:
            with transaction.atomic():
                first()
                first_done.set()
                second_started.wait(5)
                threading.Event().wait(0.4)  # keep the transaction open while `second` tries to proceed
        except Exception as exc:  # noqa: BLE001
            result["first"] = exc
        finally:
            connection.close()

    def run_second():
        first_done.wait(5)
        second_started.set()
        try:
            second()
        except Exception as exc:  # noqa: BLE001
            result["second"] = exc
        finally:
            connection.close()

    threads = [threading.Thread(target=run_first), threading.Thread(target=run_second)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(15)
    return result


@pytest.mark.django_db(transaction=True)
def test_cannot_deactivate_a_manager_while_a_report_is_being_reactivated(keep_default_company):
    sa = make_user(Role.SUPER_ADMIN)
    manager = make_user(Role.SUB_ADMIN, reports_to=sa)
    person = make_user(Role.EMPLOYEE, reports_to=manager)
    services.deactivate_user(sa, person)  # now inactive, so the manager looks free to deactivate

    result = _race(
        first=lambda: services.reactivate_user(sa, User.objects.get(pk=person.pk)),
        second=lambda: services.deactivate_user(sa, User.objects.get(pk=manager.pk)),
    )

    assert "first" not in result, result
    assert isinstance(
        result.get("second"), ValidationFailed
    )  # waited for the lock, then saw the active report
    assert User.objects.get(pk=person.pk).is_active and User.objects.get(pk=manager.pk).is_active


@pytest.mark.django_db(transaction=True)
def test_cannot_attach_someone_to_a_manager_being_deactivated(keep_default_company):
    sa = make_user(Role.SUPER_ADMIN)
    manager = make_user(Role.SUB_ADMIN, reports_to=sa)

    result = _race(
        first=lambda: services.deactivate_user(sa, User.objects.get(pk=manager.pk)),
        second=lambda: services.create_user(
            sa,
            login_id="late",
            employee_id="E-late",
            full_name="Late",
            role=Role.EMPLOYEE,
            reports_to=User.objects.get(pk=manager.pk),
            password="a-long-enough-password",
        ),  # fmt: skip
    )

    assert "first" not in result, result
    assert isinstance(result.get("second"), ValidationFailed)  # re-read the manager after the lock: inactive
    assert not User.objects.filter(login_id="late").exists()
