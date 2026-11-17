"""What the office TV shows (M10 section 3).

The data scope is deliberately small: a person's name, a task title cut to 60 characters, the due time,
the priority, a status label and whether it is overdue. Never a description, a comment or a file. This
module is the only place the board's data is read, so that scope can be checked in one place.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings

from ots.core.clock import IST, now
from ots.tasks.models import OPEN_STATUSES, AssignmentStatus, TaskAssignment, TaskEvent

SHOWN = (
    AssignmentStatus.ASSIGNED,
    AssignmentStatus.IN_PROGRESS,
    AssignmentStatus.PAUSED,
    AssignmentStatus.BLOCKED,
    AssignmentStatus.SUBMITTED,
    AssignmentStatus.CHANGES_REQUESTED,
)
LABELS = {
    AssignmentStatus.ASSIGNED: "Assigned",
    AssignmentStatus.IN_PROGRESS: "Working",
    AssignmentStatus.PAUSED: "Paused",
    AssignmentStatus.BLOCKED: "Waiting",
    AssignmentStatus.SUBMITTED: "Awaiting review",
    AssignmentStatus.CHANGES_REQUESTED: "Changes requested",
}
TITLE_MAX = 60
MAX_CARDS = 32  # 4 columns x 8 rows
MAX_APPROVALS = 12


def short(text: str, limit: int = TITLE_MAX) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


@dataclass
class Item:
    title: str
    due: datetime.datetime
    priority: str
    status: str
    overdue: bool


@dataclass
class Card:
    name: str
    top: Item
    more: int = 0
    overdue: bool = False
    items: list[Item] = field(default_factory=list)


def _item(a: TaskAssignment, at: datetime.datetime) -> Item:
    t = a.task
    return Item(
        title=short(t.title),
        due=t.due_at.astimezone(IST),
        priority=t.get_priority_display(),
        status=LABELS[AssignmentStatus(a.status)],
        overdue=t.due_at < at and a.status in OPEN_STATUSES,
    )


def build(at: datetime.datetime | None = None) -> dict[str, Any]:
    """Everything the board page needs, in one pass (two queries)."""
    at = at or now()
    local = at.astimezone(IST)
    rows = (
        TaskAssignment.objects.filter(
            status__in=SHOWN, removed_at__isnull=True, user__is_active=True,
            task__is_cancelled=False, task__hide_from_display=False,
        )
        .select_related("user", "task")
        .order_by("user__full_name", "task__due_at")
    )  # fmt: skip
    per_person: dict[Any, list[Item]] = {}
    names: dict[Any, str] = {}
    counts = {"active": 0, "review": 0, "overdue": 0}
    for a in rows[:2000]:
        item = _item(a, at)
        per_person.setdefault(a.user_id, []).append(item)
        names[a.user_id] = a.user.full_name
        counts["active"] += 1
        counts["review"] += a.status == AssignmentStatus.SUBMITTED
        counts["overdue"] += item.overdue
    cards = []
    for uid, items in per_person.items():
        items.sort(key=lambda i: (not i.overdue, i.due))  # overdue first, then the soonest
        cards.append(
            Card(name=names[uid], top=items[0], more=len(items) - 1, overdue=items[0].overdue, items=items)
        )
    cards.sort(key=lambda c: (not c.overdue, c.top.due, c.name))
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    approved = (
        TaskEvent.objects.filter(
            event_type="assignment.approved", created_at__gte=midnight, task__hide_from_display=False
        )
        .select_related("assignment__user", "task")
        .order_by("-created_at")[:MAX_APPROVALS]
    )  # fmt: skip
    return {
        "office": getattr(settings, "OTS_OFFICE_NAME", "Office"),
        "cards": cards[:MAX_CARDS],
        "hidden_cards": max(0, len(cards) - MAX_CARDS),
        "counts": counts,
        "approvals": [
            {"name": e.assignment.user.full_name if e.assignment else "", "title": short(e.task.title)}
            for e in approved
        ],
        "now": local,
    }
