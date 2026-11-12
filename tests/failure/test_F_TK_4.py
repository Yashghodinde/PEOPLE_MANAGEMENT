"""F-TK-4: an illegal transition is refused with 409 InvalidTransition; nothing changes and nothing is logged."""

import pytest
from ots.core.errors import InvalidTransition
from ots.core.models import OutboxEvent
from ots.tasks import services
from ots.tasks.models import AssignmentStatus as S
from ots.tasks.models import TaskAssignment, TaskEvent

pytestmark = pytest.mark.django_db

ILLEGAL = [
    ("ASSIGNED", "approve", "mgr"), ("ASSIGNED", "submit", "emp"), ("ASSIGNED", "unblock", "emp"),
    ("IN_PROGRESS", "start", "emp"), ("IN_PROGRESS", "approve", "mgr"), ("SUBMITTED", "start", "emp"),
    ("SUBMITTED", "submit", "emp"), ("APPROVED", "request_changes", "mgr"), ("APPROVED", "remove", "mgr"),
    ("REMOVED", "start", "emp"), ("CANCELLED", "resume", "emp"), ("BLOCKED", "submit", "emp"),
    ("PAUSED", "pause_eod", "emp"),
]  # fmt: skip
GOOD = {"remove": {"reason": "no longer needed"}, "request_changes": {"feedback": "please redo the summary"}}


@pytest.mark.parametrize("status,action,who", ILLEGAL)
def test_illegal_transitions_are_refused_and_leave_no_trace(org, assignment, status, action, who):
    TaskAssignment.objects.filter(pk=assignment.pk).update(status=status)
    a = TaskAssignment.objects.get(pk=assignment.pk)
    with pytest.raises(InvalidTransition) as exc:
        services.transition(a.pk, action, org[who], a.version, **GOOD.get(action, {}))
    assert exc.value.status == 409 and exc.value.extra["current"]["status"] == status
    after = TaskAssignment.objects.get(pk=a.pk)
    assert (after.status, after.version) == (status, a.version)
    assert (
        not TaskEvent.objects.filter(assignment=a).exists()
        and not OutboxEvent.objects.filter(type__startswith="assignment.").exists()
    )
    assert S(status)  # the status under test is a real one
