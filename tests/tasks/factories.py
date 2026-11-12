"""Build tasks directly through the ORM (the services have their own tests)."""

from __future__ import annotations

import datetime

from ots.accounts.models import User
from ots.core.clock import now
from ots.tasks.codes import next_task_code
from ots.tasks.models import AssignmentStatus, Task, TaskAssignment

from tests.accounts.factories import company


def make_task(creator: User, *assignees: User, due_in_hours: float = 48, **kw) -> Task:  # type: ignore[no-untyped-def]
    task = Task.objects.create(
        company=company(),
        task_code=kw.pop("task_code", None) or next_task_code(),
        title=kw.pop("title", "Prepare the monthly report"),
        description=kw.pop("description", "Collect the figures and send them."),
        created_by=creator,
        due_at=kw.pop("due_at", now() + datetime.timedelta(hours=due_in_hours)),
        **kw,
    )
    for user in assignees:
        TaskAssignment.objects.create(
            task=task, user=user, assigned_by=creator, status=AssignmentStatus.ASSIGNED
        )
    return task
