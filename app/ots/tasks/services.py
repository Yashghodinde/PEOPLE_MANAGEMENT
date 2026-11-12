"""All task writes (M07 sections 3 and 4): permission check, validation, the change, the event, the cache.

`transition()` is the one place an assignment changes state. It runs in one transaction (F-TK-3), takes
row locks (F-TK-2), refuses anything the table in `state.py` does not allow (F-TK-4), and publishes the
event for notifications and the TV.
"""

from __future__ import annotations

import datetime
from typing import Any

from django.db import transaction

from ots.core import events, permissions
from ots.core.clock import now
from ots.core.errors import Conflict, InvalidTransition, NotFound, PermissionDenied, ValidationFailed

from . import hooks, state
from .models import (
    AssignmentStatus,
    BlockReason,
    DerivedStatus,
    PauseReason,
    Task,
    TaskAssignment,
    TaskEvent,
)

S = AssignmentStatus


def _snapshot(a: TaskAssignment) -> dict[str, Any]:
    """The current state, returned with a 409 so the caller's screen can show what really happened."""
    return {
        "assignment_id": str(a.pk),
        "status": a.status,
        "version": a.version,
        "block_reason": a.block_reason,
        "label": state.employee_label(a.status, a.block_reason),
    }


# ---- input validation ----
def _text(inputs: dict[str, Any], key: str, minimum: int) -> str:
    value = str(inputs.get(key) or "").strip()
    if len(value) < minimum:
        raise ValidationFailed(
            f"Write at least {minimum} characters.", fields={key: [f"At least {minimum} characters."]}
        )
    return value


def _clean_inputs(trans: state.Transition, a: TaskAssignment, inputs: dict[str, Any]) -> dict[str, Any]:
    action = trans.action
    if action == "block":
        reason = str(inputs.get("block_reason") or "")
        if reason not in BlockReason.values:
            raise ValidationFailed("Choose why you are waiting.", fields={"block_reason": ["Required."]})
        return {"block_reason": reason, "block_note": _text(inputs, "block_note", state.MIN_BLOCK_NOTE)}
    if action == "submit":
        note = str(inputs.get("note") or "").strip()
        if len(note) >= state.MIN_SUBMIT_NOTE or hooks.has_submission_file(a):
            return {"note": note}
        raise ValidationFailed(
            "Attach your work or write a closing note of at least 10 characters.",
            fields={"note": [f"At least {state.MIN_SUBMIT_NOTE} characters, or attach a file."]},
        )
    if action == "request_changes":
        return {"feedback": _text(inputs, "feedback", state.MIN_FEEDBACK)}
    if action in ("remove", "cancel"):
        return {"reason": _text(inputs, "reason", state.MIN_REASON)}
    if action == "unblock":
        return {"note": str(inputs.get("note") or "").strip()}
    return {}


# ---- applying one transition ----
def _close_block(a: TaskAssignment, at: datetime.datetime) -> None:
    """End a waiting period: add its length to blocked_seconds and clear the block fields."""
    if a.blocked_since is not None:
        a.blocked_seconds += max(0, int((at - a.blocked_since).total_seconds()))
        a.blocked_since = None
    a.block_reason, a.block_note = "", ""


def _apply(
    a: TaskAssignment, task: Task, trans: state.Transition, data: dict[str, Any], at: datetime.datetime
) -> None:
    action = trans.action
    if action == "start":
        a.started_at = a.started_at or at
    elif action == "pause_eod":
        a.pause_reason = PauseReason.END_OF_DAY
    elif action == "resume":
        a.pause_reason = ""
    elif action == "block":
        a.block_reason, a.block_note, a.blocked_since, a.pause_reason = (
            data["block_reason"], data["block_note"], at, "",
        )  # fmt: skip
    elif action == "unblock":
        _close_block(a, at)
    elif action == "submit":
        a.submitted_at, a.submitted_late, a.pause_reason = at, at > task.due_at, ""
    elif action == "approve":
        a.approved_at = at
    elif action in ("remove", "cancel"):
        _close_block(a, at)
        a.pause_reason = ""
        if action == "remove":
            a.removed_at = at
    # request_changes only changes the status
    a.status = trans.target


def _write_event(
    a: TaskAssignment,
    task: Task,
    trans: state.Transition,
    actor: Any,
    before: str,
    data: dict[str, Any],
    at: datetime.datetime,
) -> None:
    payload = {"from": before, "to": a.status, "action": trans.action, "inputs": data}
    TaskEvent.objects.create(
        task=task, assignment=a, actor=actor, event_type=trans.event, payload=payload, created_at=at
    )
    events.publish(
        trans.event,
        ("assignment", str(a.pk)),
        {
            **payload,
            "task_id": str(task.pk),
            "task_code": task.task_code,
            "assignee_id": str(a.user_id),
            "creator_id": str(task.created_by_id),
            "manager_id": str(a.user.reports_to_id) if a.user.reports_to_id else None,
            "actor_id": str(actor.pk) if actor is not None else None,
            "priority": task.priority,
        },
    )


def recompute_task(task: Task, at: datetime.datetime) -> Task:
    """Refresh the cached status and flags from the assignments, in the same transaction as the change."""
    statuses = list(TaskAssignment.objects.filter(task=task).values_list("status", flat=True))
    task.derived_status, task.has_blocked = state.derive_status(statuses, task.is_cancelled)
    if task.derived_status == DerivedStatus.COMPLETED:
        task.completed_at = task.completed_at or at
    else:
        task.completed_at = None  # a new assignee on a finished task reopens it
    task.version += 1
    task.save(update_fields=["derived_status", "has_blocked", "completed_at", "version"])
    return task


@transaction.atomic
def transition(
    assignment_id: Any,
    action: str,
    actor: Any,
    expected_version: int | None = None,
    *,
    system: bool = False,
    **inputs: Any,
) -> TaskAssignment:
    """Move one assignment along the state table. `system=True` is for the end-of-day closure only."""
    trans = state.TRANSITIONS.get(action)
    if trans is None:
        raise InvalidTransition(f"Unknown action: {action}")
    try:
        a = TaskAssignment.objects.select_for_update().select_related("user").get(pk=assignment_id)
    except TaskAssignment.DoesNotExist:
        raise NotFound("Assignment not found.") from None
    task = Task.objects.select_for_update().get(pk=a.task_id)
    a.task = task
    if expected_version is not None and a.version != expected_version:  # F-TK-2
        raise Conflict("This was changed by someone else. Reload and try again.", current=_snapshot(a))
    if system:
        if "system" not in trans.actors:
            raise PermissionDenied(f"The system may not {action}.", action=f"assignment.{action}")
    else:
        permissions.require(actor, f"assignment.{action}", a)
    if task.is_cancelled or a.status not in trans.sources:  # F-TK-4
        raise InvalidTransition(
            f"This work is {state.employee_label(a.status, a.block_reason).lower()}; "
            f"it cannot be changed with '{action.replace('_', ' ')}' now.",
            current=_snapshot(a),
        )
    data = _clean_inputs(trans, a, inputs)
    at, before = now(), a.status
    _apply(a, task, trans, data, at)
    a.version += 1
    a.save()
    _write_event(a, task, trans, actor, before, data, at)
    recompute_task(task, at)
    return a
