"""F-TK-2: two people change the same assignment at once. One wins; the other gets a 409 with the current state."""

import threading

import pytest
from django.db import connection
from ots.accounts.models import Company, Role
from ots.core.errors import Conflict
from ots.tasks import services
from ots.tasks.models import AssignmentStatus as S
from ots.tasks.models import TaskAssignment

from tests.accounts.factories import make_user
from tests.tasks.factories import make_task


@pytest.fixture
def keep_default_company():
    yield
    Company.objects.get_or_create(code="DEFAULT", defaults={"name": "Default company"})


@pytest.mark.django_db(transaction=True)
def test_two_simultaneous_changes_with_the_same_version_one_wins_one_gets_409(keep_default_company):
    sa = make_user(Role.SUPER_ADMIN)
    mgr = make_user(Role.SUB_ADMIN, reports_to=sa)
    emp = make_user(Role.EMPLOYEE, reports_to=mgr)
    a = make_task(mgr, emp).assignments.get()
    results: list[object] = []

    def worker():
        try:
            results.append(services.transition(a.pk, "start", emp, expected_version=a.version))
        except BaseException as exc:  # noqa: BLE001
            results.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    winners = [r for r in results if isinstance(r, TaskAssignment)]
    losers = [r for r in results if isinstance(r, Conflict)]
    assert len(winners) == 1 and len(losers) == 1, results
    assert losers[0].status == 409 and losers[0].extra["current"]["status"] == S.IN_PROGRESS
    assert losers[0].extra["current"]["version"] == a.version + 1
    final = TaskAssignment.objects.get(pk=a.pk)
    assert final.status == S.IN_PROGRESS and final.version == a.version + 1  # applied exactly once
