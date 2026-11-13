"""Timeline providers for the activity timeline (M03): work sessions (M06) and task events (M07)."""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from ots.audit.timeline import Cursor, Event, _apply_window, bigint_key, register_provider
from ots.core.clock import IST
from ots.tasks.models import TaskEvent

from .models import WorkSession


def session_provider(
    user_id: Any,
    start: datetime.datetime | None,
    end: datetime.datetime | None,
    cursor: Cursor | None,
    limit: int,
) -> list[Event]:
    qs = _apply_window(WorkSession.objects.filter(user_id=user_id), "login_at", start, end)
    if cursor is not None:
        qs = qs.filter(cursor.older_than("session", "login_at", "id", uuid.UUID))
    out = []
    for s in qs.order_by("-login_at", "-id")[:limit]:
        ended = (
            f"ended {s.logout_at.astimezone(IST):%H:%M} ({s.get_logout_reason_display()})"
            if s.logout_at
            else "still open"
        )
        out.append(Event(at=s.login_at, source="session", key=s.id, title="Started work", detail=ended))
    return out


_TASK_TITLES = {
    "assignment.created": "Given a task",
    "assignment.started": "Started a task",
    "assignment.paused": "Paused a task for the day",
    "assignment.resumed": "Resumed a task",
    "assignment.blocked": "Marked a task as waiting",
    "assignment.unblocked": "Back to work on a task",
    "assignment.submitted": "Completed a task",
    "assignment.changes_requested": "Changes requested on a task",
    "assignment.approved": "Work approved",
    "assignment.removed": "Removed from a task",
    "comment.added": "Wrote on a task",
    "task.created": "Created a task",
    "task.cancelled": "Cancelled a task",
}


def task_provider(
    user_id: Any,
    start: datetime.datetime | None,
    end: datetime.datetime | None,
    cursor: Cursor | None,
    limit: int,
) -> list[Event]:
    from django.db.models import Q

    qs = TaskEvent.objects.filter(Q(actor_id=user_id) | Q(assignment__user_id=user_id)).select_related("task")
    qs = _apply_window(qs, "created_at", start, end)
    if cursor is not None:
        qs = qs.filter(cursor.older_than("task", "created_at", "id", bigint_key))
    return [
        Event(
            at=e.created_at,
            source="task",
            key=e.id,
            title=_TASK_TITLES.get(e.event_type, e.event_type),
            detail=f"{e.task.task_code} {e.task.title}",
            url=f"/app/tasks/{e.task_id}",
        )  # fmt: skip
        for e in qs.order_by("-created_at", "-id")[:limit]
    ]


register_provider("session", session_provider)
register_provider("task", task_provider)
