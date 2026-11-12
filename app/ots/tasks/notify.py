"""Who hears about what (M10 section 2). Called from the task services inside their transaction."""

from __future__ import annotations

from typing import Any

from ots.notifications import services as notifications
from ots.notifications.models import Kind

from .models import AssignmentStatus, Task, TaskAssignment


def _preview(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= 80 else text[:77] + "..."


def after_event(
    task: Task, actor: Any, event_type: str, a: TaskAssignment | None, payload: dict[str, Any]
) -> None:
    """Turn one task event into notifications for the right people."""
    inputs = payload.get("inputs") or {}
    if a is not None:
        who, boss, code = a.user.full_name, a.user.reports_to_id, task.task_code
        owner, reviewers = [a.user_id], [task.created_by_id, boss]
        table: dict[str, tuple[list[Any], str, str, str]] = {
            "assignment.created": (owner, Kind.ASSIGNED, f"New task for you: {task.title}", ""),
            "assignment.submitted": (reviewers, Kind.SUBMITTED, f"{who} finished work on {code}", task.title),
            "assignment.changes_requested": (
                owner,
                Kind.CHANGES_REQUESTED,
                f"Changes requested on {code}",
                _preview(str(inputs.get("feedback", ""))),
            ),
            "assignment.approved": (owner, Kind.APPROVED, f"Your work on {code} was approved", task.title),
            "assignment.blocked": (
                reviewers,
                Kind.BLOCKED,
                f"{who} is waiting on {code}",
                _preview(str(inputs.get("block_note", ""))),
            ),
            "assignment.removed": (owner, Kind.REMOVED, f"You were removed from {code}", task.title),
        }
        if event_type in table:
            users, kind, title, body = table[event_type]
            notifications.notify(users, kind, task, title, actor=actor, body=body)
    elif event_type == "task.cancelled":
        people = list(task.assignments.filter(removed_at__isnull=True).values_list("user_id", flat=True))
        notifications.notify(
            people, Kind.CANCELLED, task, f"{task.task_code} was cancelled", actor=actor, body=task.title
        )
    elif event_type == "task.deadline_changed":
        people = list(task.assignments.filter(removed_at__isnull=True).values_list("user_id", flat=True))
        notifications.notify(
            people, Kind.CHANGED, task, f"New deadline for {task.task_code}", actor=actor, body=task.title
        )


def task_completed(task: Task, actor: Any) -> None:
    """Every active person approved: tell the person who set the task."""
    live = task.assignments.exclude(status__in=(AssignmentStatus.REMOVED, AssignmentStatus.CANCELLED))
    if live.exists():
        notifications.notify(
            [task.created_by_id],
            Kind.COMPLETED,
            task,
            f"{task.task_code} is complete",
            actor=actor,
            body=task.title,
        )


def comment_added(task: Task, actor: Any, comment: Any) -> None:
    people = set(task.assignments.filter(removed_at__isnull=True).values_list("user_id", flat=True))
    people.add(task.created_by_id)
    notifications.notify(
        people, Kind.COMMENT, task, f"{actor.full_name} wrote on {task.task_code}",
        actor=actor, comment=comment, body=_preview(comment.body),
    )  # fmt: skip
