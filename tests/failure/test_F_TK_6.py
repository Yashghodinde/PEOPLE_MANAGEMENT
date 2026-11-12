"""F-TK-6: the manager changes after a task was assigned. Approvals use the CURRENT manager; reports about the past
still attribute the work to the manager at that time."""

import datetime

import pytest
from freezegun import freeze_time
from ots.accounts import services as people
from ots.accounts.models import User
from ots.core.errors import PermissionDenied
from ots.tasks import selectors, services
from ots.tasks.models import AssignmentStatus as S
from ots.tasks.models import TaskAssignment

from tests.tasks.factories import make_task

pytestmark = pytest.mark.django_db
NOTE = {"note": "Everything is done and checked."}


def test_after_a_manager_change_the_new_manager_can_approve_and_the_old_one_cannot(org):
    # `sa` (not the manager) created the task, so the manager link alone decides who may review
    task = make_task(org["sa"], org["emp"])
    a = task.assignments.get()
    TaskAssignment.objects.filter(pk=a.pk).update(status=S.IN_PROGRESS)
    a.refresh_from_db()
    a = services.transition(a.pk, "submit", org["emp"], a.version, **NOTE)

    people.set_manager(org["sa"], org["emp"], org["other"])  # Asha now reports to Neha, not Arjun
    with pytest.raises(PermissionDenied):
        services.transition(a.pk, "approve", org["mgr"], a.version)  # the old manager is out
    approved = services.transition(a.pk, "approve", org["other"], a.version)  # the current manager decides
    assert approved.status == S.APPROVED


def test_the_creator_keeps_review_rights_whatever_happens_to_the_reporting_line(org):
    task = make_task(org["mgr"], org["emp"])  # Arjun created it
    a = task.assignments.get()
    TaskAssignment.objects.filter(pk=a.pk).update(status=S.SUBMITTED)
    a.refresh_from_db()
    people.set_manager(org["sa"], org["emp"], org["other"])
    assert (
        services.transition(a.pk, "approve", org["mgr"], a.version).status == S.APPROVED
    )  # still his own task


def test_the_assignment_remembers_who_assigned_it_forever(org):
    task = make_task(org["mgr"], org["emp"])
    people.set_manager(org["sa"], org["emp"], org["other"])
    assert task.assignments.get().assigned_by_id == org["mgr"].pk


def test_a_past_period_report_attributes_the_work_to_the_manager_of_that_time(org):
    with freeze_time("2030-01-10 06:00:00"):
        task = make_task(org["sa"], org["emp"])  # assigned while Arjun was the manager
    with freeze_time("2030-01-12 06:00:00"):
        people.set_manager(org["sa"], User.objects.get(pk=org["emp"].pk), org["other"])
    day = datetime.date
    old_then = selectors.team_assignments_on(org["mgr"].pk, day(2030, 1, 11))
    assert [a.task_id for a in old_then] == [task.pk]  # on the 11th Asha was Arjun's
    assert not selectors.team_assignments_on(org["other"].pk, day(2030, 1, 11)).exists()
    assert not selectors.team_assignments_on(
        org["mgr"].pk, day(2030, 1, 13)
    ).exists()  # from the 12th she is Neha's
    assert [a.task_id for a in selectors.team_assignments_on(org["other"].pk, day(2030, 1, 13))] == [task.pk]
    assert not selectors.team_assignments_on(
        org["mgr"].pk, day(2030, 1, 9)
    ).exists()  # before it was assigned


def test_an_assignment_removed_before_the_day_is_not_counted_that_day(org):
    with freeze_time("2030-01-10 06:00:00"):
        task = make_task(org["sa"], org["emp"])
        a = task.assignments.get()
    with freeze_time("2030-01-11 06:00:00"):
        services.transition(a.pk, "remove", org["sa"], a.version, reason="moved to another team")
    assert selectors.team_assignments_on(org["mgr"].pk, datetime.date(2030, 1, 10)).count() == 1
    assert not selectors.team_assignments_on(org["mgr"].pk, datetime.date(2030, 1, 12)).exists()
