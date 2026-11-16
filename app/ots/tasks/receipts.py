"""Delivery receipts (D-052): did a key change reach the person, and did they open it?

A receipt only informs. It is created in the same transaction as the event it is about, and nothing here can
block or change a task. Times are always the server's: the client only says "I got it".
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Iterable
from typing import Any

from django.conf import settings
from django.db import transaction
from django.http import HttpRequest

from ots.accounts.models import Role, User
from ots.core.clock import now
from ots.core.errors import ValidationFailed
from ots.core.permissions import policy, require

from .models import AssignmentStatus, EventReceipt, Priority, Task, TaskAssignment, TaskEvent

# Changes the person must not miss. Anything at all on an URGENT task counts too.
KEY_EVENTS = (
    "assignment.created",
    "assignment.removed",
    "task.cancelled",
    "task.deadline_changed",
    "assignment.changes_requested",
)
MAX_ACK = 100
INACTIVE = (AssignmentStatus.REMOVED, AssignmentStatus.CANCELLED)


def late_after() -> datetime.timedelta:
    return datetime.timedelta(seconds=int(getattr(settings, "OTS_RECEIPT_LATE_SECONDS", 120)))


def urgent_after() -> datetime.timedelta:
    return datetime.timedelta(seconds=int(getattr(settings, "OTS_RECEIPT_URGENT_SECONDS", 900)))


@policy("receipt.ack")
def can_ack(user: Any, receipt: EventReceipt | None) -> bool:
    """Only the person the receipt is for may say they got it."""
    return bool(
        user and getattr(user, "is_active", False) and receipt is not None and receipt.recipient_id == user.pk
    )


def _recipients(task: Task, actor: Any, event_type: str, a: TaskAssignment | None) -> set[Any]:
    """Who the change is for (the same people the notification goes to), never the person who made it."""
    owner: set[Any] = {a.user_id} if a is not None else set()
    reviewers: set[Any] = {task.created_by_id, a.user.reports_to_id} if a is not None else set()
    if event_type in ("assignment.created", "assignment.removed", "assignment.changes_requested"):
        people = owner
    elif event_type in ("assignment.submitted", "assignment.blocked"):
        people = reviewers
    else:  # task-level changes: everyone still working on it
        people = set(
            task.assignments.exclude(status__in=INACTIVE)
            .filter(removed_at__isnull=True)
            .values_list("user_id", flat=True)
        )
    people.discard(None)
    if actor is not None:
        people.discard(actor.pk)
    return people


def create_for(
    event: TaskEvent, task: Task, actor: Any, event_type: str, a: TaskAssignment | None
) -> list[EventReceipt]:
    """Make the receipt rows for one event (inside its transaction) and tell each recipient's open page."""
    urgent = task.priority == Priority.URGENT
    if event_type not in KEY_EVENTS and not urgent:
        return []
    ids = _recipients(task, actor, event_type, a)
    active = set(User.objects.filter(pk__in=ids, is_active=True).values_list("pk", flat=True))
    rows = [
        EventReceipt(
            event=event, task=task, assignment=a, sender=actor, recipient_id=u, urgent=urgent,
            what=event_type, created_at=event.created_at,
        )
        for u in sorted(active, key=str)
    ]  # fmt: skip
    EventReceipt.objects.bulk_create(rows, ignore_conflicts=True)
    if rows:
        from ots.live.publisher import send

        for r in rows:  # one message each, so the page can answer at once ("no timer, no batching")
            send(users=[r.recipient_id], type_="receipt", receipt_id=r.pk)
    return rows


def _tell_senders(task_ids: Iterable[Any]) -> None:
    """The sender's page re-fetches, so its tick turns into two."""
    from ots.live.publisher import send, task_audience

    for tid in set(task_ids):
        send(users=task_audience(tid), roles=["SUPER_ADMIN"], type_="task-changed", task_id=tid)


def _ids(raw: Any) -> list[uuid.UUID]:
    if not isinstance(raw, (list, tuple)):
        raise ValidationFailed("ids must be a list.", fields={"ids": ["Invalid."]})
    if len(raw) > MAX_ACK:
        raise ValidationFailed(f"At most {MAX_ACK} at once.", fields={"ids": ["Too many."]})
    try:
        return [uuid.UUID(str(x)) for x in raw]
    except ValueError:
        raise ValidationFailed("That id is not valid.", fields={"ids": ["Invalid."]}) from None


def ack(user: Any, raw_ids: Any, request: HttpRequest | None = None) -> int:
    """The recipient's page says it received these. Idempotent; another person's receipt is refused."""
    ids = _ids(raw_ids)
    if not ids:
        return 0
    with transaction.atomic():
        rows = list(EventReceipt.objects.select_for_update().filter(pk__in=ids))
        for r in rows:
            require(user, "receipt.ack", r)  # not yours: refused and recorded as security.denied
        at = now()
        fresh = [r for r in rows if r.delivered_at is None]
        if fresh:
            EventReceipt.objects.filter(pk__in=[r.pk for r in fresh]).update(delivered_at=at)
            _tell_senders(r.task_id for r in fresh)
    return len(fresh)


def mark_seen(user: Any, task: Task) -> int:
    """The recipient opened the task: everything sent to them about it is seen (and so delivered)."""
    waiting = EventReceipt.objects.filter(task=task, recipient=user, seen_at__isnull=True)
    if not waiting.exists():  # nearly every page view: one cheap read
        return 0
    with transaction.atomic():
        at = now()
        rows = list(
            EventReceipt.objects.select_for_update().filter(task=task, recipient=user, seen_at__isnull=True)
        )
        for r in rows:
            r.seen_at = at
            if r.delivered_at is None:
                r.delivered_at = at
            r.save(update_fields=["seen_at", "delivered_at"])
        if rows:
            _tell_senders([task.pk])
    return len(rows)


def pending_ids(user: Any) -> list[str]:
    """What this person has not acknowledged yet: after a reconnect the page asks, then acks."""
    qs = EventReceipt.objects.filter(recipient=user, delivered_at__isnull=True).order_by("created_at")
    return [str(pk) for pk in qs.values_list("pk", flat=True)[:MAX_ACK]]


# --- what the sender sees ---


def marks_for(viewer: Any, task: Task) -> dict[Any, dict[str, str]]:
    """{recipient id: mark} from the latest receipt per person, for the people on this task.

    Only managers see them (the person it was sent to does not). States: saved, late ("Not delivered yet"),
    delivered, seen.
    """
    if viewer is None or viewer.role == Role.EMPLOYEE:
        return {}
    at = now()
    out: dict[Any, dict[str, str]] = {}
    for r in EventReceipt.objects.filter(task=task).order_by("-created_at", "-id")[:200]:
        if r.recipient_id in out:
            continue
        if r.seen_at is not None:
            state, text = "seen", "Seen"
        elif r.delivered_at is not None:
            state, text = "delivered", "Delivered"
        elif at - r.created_at >= late_after():
            state, text = "late", "Not delivered yet"
        else:
            state, text = "saved", "Sent"
        out[r.recipient_id] = {"state": state, "text": text}
    return out


def undelivered(viewer: Any, limit: int = 20) -> dict[str, Any]:
    """For a manager's dashboard: changes that have still not reached someone (older than the late mark)."""
    from ots.accounts import selectors as accounts

    if viewer is None or viewer.role == Role.EMPLOYEE:
        return {"count": 0, "rows": []}
    qs = EventReceipt.objects.filter(delivered_at__isnull=True, created_at__lte=now() - late_after())
    if viewer.role == Role.SUB_ADMIN:
        qs = qs.filter(recipient_id__in=accounts.subtree_ids(viewer.pk))
    rows = list(qs.select_related("task", "recipient").order_by("created_at")[:limit])
    # one query in the usual case: a full page is the only time the true total needs asking for
    return {"count": qs.count() if len(rows) == limit else len(rows), "rows": rows}


def remind_senders(at: Any = None) -> int:
    """URGENT changes still not delivered after 15 minutes: tell the sender once (job, every minute)."""
    from ots.notifications import services as notifications
    from ots.notifications.models import Kind

    at = at or now()
    made = 0
    due = EventReceipt.objects.filter(
        urgent=True, delivered_at__isnull=True, undelivered_notified_at__isnull=True,
        created_at__lte=at - urgent_after(), sender__isnull=False,
    ).select_related("task", "recipient")  # fmt: skip
    for r in due[:200]:
        with transaction.atomic():
            made += len(
                notifications.notify(
                    [r.sender_id], Kind.UNDELIVERED, r.task, f"Not received yet: {r.recipient.full_name}",
                    body=f"{r.task.task_code} change has not reached them", source=f"undelivered:{r.pk}",
                )
            )  # fmt: skip
            EventReceipt.objects.filter(pk=r.pk).update(undelivered_notified_at=at)
    return made
