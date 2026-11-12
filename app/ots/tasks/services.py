"""All task writes (M07 sections 3 and 4): permission check, validation, the change, the event, the cache.

`transition()` is the one place an assignment changes state. It runs in one transaction (F-TK-3), takes
row locks (F-TK-2), refuses anything the table in `state.py` does not allow (F-TK-4), and publishes the
event for notifications and the TV.
"""

from __future__ import annotations

import datetime
from typing import Any

from django.db import transaction

from ots.accounts.models import User
from ots.core import events, permissions
from ots.core.clock import now
from ots.core.errors import Conflict, InvalidTransition, NotFound, PermissionDenied, ValidationFailed

from . import hooks, selectors, state
from .codes import next_task_code
from .models import (
    AssignmentStatus,
    BlockReason,
    DerivedStatus,
    PauseReason,
    Priority,
    Task,
    TaskAssignment,
    TaskComment,
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


def _record(
    task: Task,
    actor: Any,
    event_type: str,
    payload: dict[str, Any],
    at: datetime.datetime,
    a: TaskAssignment | None = None,
    **extra: Any,
) -> None:
    """One history row plus the outbox event that drives notifications, receipts and the TV."""
    TaskEvent.objects.create(
        task=task, assignment=a, actor=actor, event_type=event_type, payload=payload, created_at=at
    )
    body: dict[str, Any] = {
        **payload,
        "task_id": str(task.pk),
        "task_code": task.task_code,
        "creator_id": str(task.created_by_id),
        "actor_id": str(actor.pk) if actor is not None else None,
        "priority": task.priority,
        **extra,
    }
    if a is not None:
        body["assignment_id"] = str(a.pk)
        body["assignee_id"] = str(a.user_id)
        body["manager_id"] = str(a.user.reports_to_id) if a.user.reports_to_id else None
    events.publish(event_type, ("assignment", str(a.pk)) if a is not None else ("task", str(task.pk)), body)


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
    _record(task, actor, trans.event, payload, at, a)


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
    _move(a, task, trans, actor, inputs)
    recompute_task(task, now())
    return a


def _move(a: TaskAssignment, task: Task, trans: state.Transition, actor: Any, inputs: dict[str, Any]) -> None:
    """Source check, input validation, apply, event. The caller has already locked, version-checked and
    permission-checked, and recomputes the task afterwards."""
    if task.is_cancelled or a.status not in trans.sources:  # F-TK-4
        raise InvalidTransition(
            f"This work is {state.employee_label(a.status, a.block_reason).lower()}; "
            f"it cannot be changed with '{trans.action.replace('_', ' ')}' now.",
            current=_snapshot(a),
        )
    data = _clean_inputs(trans, a, inputs)
    at, before = now(), a.status
    _apply(a, task, trans, data, at)
    a.version += 1
    a.save()
    _write_event(a, task, trans, actor, before, data, at)


# ---- task-level services ----
MAX_TITLE = 200
MAX_COMMENT = 4000
EDITABLE = (
    "title",
    "description",
    "priority",
    "category",
    "due_at",
    "scheduled_start_at",
    "hide_from_display",
)


def _lock_task(task_id: Any) -> Task:
    try:
        return Task.objects.select_for_update().get(pk=task_id)
    except Task.DoesNotExist:
        raise NotFound("Task not found.") from None


def _check_task_version(task: Task, expected: int | None) -> None:
    if expected is not None and task.version != expected:
        raise Conflict(
            "This task was changed by someone else. Reload and try again.",
            current={"task_id": str(task.pk), "version": task.version, "status": task.derived_status},
        )


def _check_text(value: Any, field: str, label: str, maximum: int, *, required: bool = True) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise ValidationFailed(f"Enter {label}.", fields={field: ["Required."]})
    if len(text) > maximum:
        raise ValidationFailed(
            f"{label.capitalize()} is too long.", fields={field: [f"At most {maximum} characters."]}
        )
    return text


def _check_times(due_at: Any, start_at: Any, at: datetime.datetime, *, due_changed: bool = True) -> None:
    if due_at is None or not isinstance(due_at, datetime.datetime) or due_at.tzinfo is None:
        raise ValidationFailed("Choose a due date and time.", fields={"due_at": ["Required."]})
    if due_changed and due_at <= at:
        raise ValidationFailed(
            "The due time must be in the future.", fields={"due_at": ["Must be in the future."]}
        )
    if start_at is not None and (start_at.tzinfo is None or start_at >= due_at):
        raise ValidationFailed(
            "The start time must be before the due time.",
            fields={"scheduled_start_at": ["Must be before due."]},
        )


def _resolve_assignees(actor: Any, assignee_ids: Any) -> list[User]:
    """Lock and validate the people: at least one, all active, and all within what `actor` may assign."""
    ids = list(dict.fromkeys(assignee_ids or ()))  # keep order, drop repeats
    if not ids:
        raise ValidationFailed("Choose at least one person.", fields={"assignees": ["Required."]})
    found = {u.pk: u for u in User.objects.select_for_update().filter(pk__in=ids)}
    people: list[User] = []
    for raw in ids:
        user = next((u for k, u in found.items() if str(k) == str(raw)), None)
        if user is None:
            raise ValidationFailed("That person does not exist.", fields={"assignees": ["Unknown person."]})
        if not user.is_active or user.is_break_glass:  # F-TK-5
            raise ValidationFailed(
                f"{user.full_name} is not active, so work cannot be given to them.",
                fields={"assignees": [f"{user.full_name} is inactive."]},
            )
        people.append(user)
    allowed = selectors.assignable_ids(actor)
    outside = [u for u in people if u.pk not in allowed]
    if outside:
        raise PermissionDenied("You can give work only to people in your own team.", action="assignment.add")
    return people


def _new_assignment(task: Task, actor: Any, user: User, at: datetime.datetime) -> TaskAssignment:
    a = TaskAssignment.objects.create(task=task, user=user, assigned_by=actor, assigned_at=at)
    a.user = user
    _record(task, actor, "assignment.created", {"assignee": user.full_name}, at, a)
    return a


@transaction.atomic
def create_task(
    actor: Any,
    *,
    title: str,
    due_at: datetime.datetime,
    assignee_ids: Any,
    description: str = "",
    priority: str = Priority.NORMAL,
    category: str = "",
    scheduled_start_at: datetime.datetime | None = None,
    hide_from_display: bool = False,
    reference_file_ids: Any = (),
) -> Task:
    permissions.require(actor, "task.create")
    at = now()
    title = _check_text(title, "title", "a title", MAX_TITLE)
    description = _check_text(description, "description", "the description", 20000, required=False)
    category = _check_text(category, "category", "the category", 60, required=False)
    if priority not in Priority.values:
        raise ValidationFailed("Choose a priority.", fields={"priority": ["Invalid."]})
    _check_times(due_at, scheduled_start_at, at)
    people = _resolve_assignees(actor, assignee_ids)
    task = Task.objects.create(
        company=actor.company,
        task_code=next_task_code(),
        title=title,
        description=description,
        priority=priority,
        category=category,
        created_by=actor,
        created_at=at,
        scheduled_start_at=scheduled_start_at,
        due_at=due_at,
        hide_from_display=hide_from_display,
    )
    hooks.link_reference_files(task, reference_file_ids, actor)
    _record(
        task,
        actor,
        "task.created",
        {
            "title": title,
            "priority": priority,
            "due_at": due_at.isoformat(),
            "assignees": [u.full_name for u in people],
        },
        at,
    )
    for user in people:
        _new_assignment(task, actor, user, at)
    return task


@transaction.atomic
def update_task(actor: Any, task_id: Any, expected_version: int | None = None, **changes: Any) -> Task:
    """Edit title, description, priority, category, due time, start time or TV visibility. Logs the diff; a
    deadline change also logs `task.deadline_changed` (a key event for delivery receipts)."""
    unknown = set(changes) - set(EDITABLE)
    if unknown:
        raise ValidationFailed(f"Unknown fields: {', '.join(sorted(unknown))}")
    task = _lock_task(task_id)
    _check_task_version(task, expected_version)
    permissions.require(actor, "task.update", task)
    if task.is_cancelled:
        raise InvalidTransition("A cancelled task cannot be edited.")
    at = now()
    clean: dict[str, Any] = {}
    if "title" in changes:
        clean["title"] = _check_text(changes["title"], "title", "a title", MAX_TITLE)
    if "description" in changes:
        clean["description"] = _check_text(
            changes["description"], "description", "the description", 20000, required=False
        )
    if "category" in changes:
        clean["category"] = _check_text(changes["category"], "category", "the category", 60, required=False)
    if "priority" in changes:
        if changes["priority"] not in Priority.values:
            raise ValidationFailed("Choose a priority.", fields={"priority": ["Invalid."]})
        clean["priority"] = changes["priority"]
    if "hide_from_display" in changes:
        clean["hide_from_display"] = bool(changes["hide_from_display"])
    new_due = changes.get("due_at", task.due_at)
    new_start = changes.get("scheduled_start_at", task.scheduled_start_at)
    if "due_at" in changes or "scheduled_start_at" in changes:
        _check_times(new_due, new_start, at, due_changed=new_due != task.due_at)
        clean["due_at"], clean["scheduled_start_at"] = new_due, new_start
    diff = {k: [getattr(task, k), v] for k, v in clean.items() if getattr(task, k) != v}
    if not diff:
        return task
    old_due = task.due_at
    for k, (_, new) in diff.items():
        setattr(task, k, new)
    task.version += 1
    task.save()
    shown = {k: [_plain(old), _plain(new)] for k, (old, new) in diff.items()}
    _record(task, actor, "task.updated", {"changes": shown}, at)
    if "due_at" in diff:
        _record(
            task,
            actor,
            "task.deadline_changed",
            {"from": old_due.isoformat(), "to": task.due_at.isoformat()},
            at,
        )
    return task


def _plain(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime.datetime) else value


@transaction.atomic
def add_assignees(
    actor: Any, task_id: Any, assignee_ids: Any, expected_version: int | None = None
) -> list[TaskAssignment]:
    task = _lock_task(task_id)
    _check_task_version(task, expected_version)
    permissions.require(actor, "assignment.add", task)
    if task.is_cancelled:
        raise InvalidTransition("A cancelled task cannot take new people.")
    people = _resolve_assignees(actor, assignee_ids)
    live = set(task.assignments.filter(removed_at__isnull=True).values_list("user_id", flat=True))
    already = [u.full_name for u in people if u.pk in live]
    if already:
        raise ValidationFailed(
            f"{', '.join(already)} already on this task.", fields={"assignees": ["Already on this task."]}
        )
    at = now()
    created = [_new_assignment(task, actor, user, at) for user in people]
    recompute_task(task, at)  # a finished task is reopened by a new person
    return created


@transaction.atomic
def reassign(
    actor: Any, assignment_id: Any, new_user_id: Any, reason: str, expected_version: int | None = None
) -> TaskAssignment:
    """Take the work from one person and give it to another, in one transaction."""
    try:
        old = TaskAssignment.objects.select_for_update().select_related("user").get(pk=assignment_id)
    except TaskAssignment.DoesNotExist:
        raise NotFound("Assignment not found.") from None
    task = _lock_task(old.task_id)
    old.task = task
    if expected_version is not None and old.version != expected_version:
        raise Conflict("This was changed by someone else. Reload and try again.", current=_snapshot(old))
    permissions.require(actor, "assignment.remove", old)
    permissions.require(actor, "assignment.add", task)
    (person,) = _resolve_assignees(actor, [new_user_id])
    if task.assignments.filter(user=person, removed_at__isnull=True).exists():
        raise ValidationFailed(
            f"{person.full_name} is already on this task.", fields={"assignees": ["Already on this task."]}
        )
    _move(old, task, state.TRANSITIONS["remove"], actor, {"reason": reason or "reassigned"})
    new = _new_assignment(task, actor, person, now())
    recompute_task(task, now())
    return new


@transaction.atomic
def cancel_task(actor: Any, task_id: Any, reason: str, expected_version: int | None = None) -> Task:
    task = _lock_task(task_id)
    _check_task_version(task, expected_version)
    permissions.require(actor, "task.cancel", task)
    if task.is_cancelled:
        raise InvalidTransition("This task is already cancelled.")
    reason = _text({"reason": reason}, "reason", state.MIN_REASON)
    open_ones = list(
        TaskAssignment.objects.select_for_update()
        .select_related("user")
        .filter(task=task, status__in=state.NON_FINAL)
    )
    for a in open_ones:
        a.task = task
        _move(a, task, state.TRANSITIONS["cancel"], actor, {"reason": reason})
    at = now()
    task.is_cancelled = True
    task.save(update_fields=["is_cancelled"])
    _record(task, actor, "task.cancelled", {"reason": reason, "cancelled_assignments": len(open_ones)}, at)
    recompute_task(task, at)
    return task


@transaction.atomic
def add_comment(actor: Any, task_id: Any, body: str, assignment_id: Any = None) -> TaskComment:
    """Comments are immutable. Anyone who may see the discussion can add one, also on a cancelled task."""
    try:
        task = Task.objects.get(pk=task_id)
    except Task.DoesNotExist:
        raise NotFound("Task not found.") from None
    permissions.require(actor, "task.comment", task)
    text = _check_text(body, "body", "a comment", MAX_COMMENT)
    assignment = None
    if assignment_id is not None:
        assignment = TaskAssignment.objects.filter(pk=assignment_id, task=task).select_related("user").first()
        if assignment is None:
            raise ValidationFailed(
                "That assignment is not on this task.", fields={"assignment": ["Invalid."]}
            )
    at = now()
    comment = TaskComment.objects.create(
        task=task, assignment=assignment, author=actor, body=text, created_at=at
    )
    _record(
        task, actor, "comment.added", {"comment_id": str(comment.pk), "length": len(text)}, at, assignment
    )
    return comment
