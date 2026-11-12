"""F-TK-3: a crash in the middle of a transition. Everything rolls back: no half-applied change, no orphan event."""

import pytest
from ots.core import events
from ots.core.models import OutboxEvent
from ots.tasks import services
from ots.tasks.models import AssignmentStatus as S
from ots.tasks.models import Task, TaskAssignment, TaskEvent

pytestmark = pytest.mark.django_db


def test_a_failure_after_the_event_row_rolls_the_whole_change_back(org, task, assignment, monkeypatch):
    before_task = Task.objects.get(pk=task.pk)

    def explode(*args, **kwargs):
        raise RuntimeError("the server died while publishing")

    monkeypatch.setattr(events, "publish", explode)  # runs AFTER the TaskEvent row was written
    with pytest.raises(RuntimeError):
        services.transition(assignment.pk, "start", org["emp"], assignment.version)
    a = TaskAssignment.objects.get(pk=assignment.pk)
    after_task = Task.objects.get(pk=task.pk)
    assert (a.status, a.version, a.started_at) == (S.ASSIGNED, assignment.version, None)
    assert (after_task.derived_status, after_task.version) == (
        before_task.derived_status,
        before_task.version,
    )
    assert not TaskEvent.objects.filter(assignment=assignment).exists()
    assert not OutboxEvent.objects.filter(type="assignment.started").exists()


def test_a_failure_while_recomputing_the_task_rolls_back_too(org, task, assignment, monkeypatch):
    def explode(*args, **kwargs):
        raise RuntimeError("could not update the task summary")

    monkeypatch.setattr(services, "recompute_task", explode)
    with pytest.raises(RuntimeError):
        services.transition(assignment.pk, "start", org["emp"], assignment.version)
    a = TaskAssignment.objects.get(pk=assignment.pk)
    assert a.status == S.ASSIGNED and not TaskEvent.objects.filter(assignment=assignment).exists()
