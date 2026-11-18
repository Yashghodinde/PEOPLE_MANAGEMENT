"""Reads behind the card landing (IMP-09): who assigned work to whom, counted, ordered, and paged.

Every number comes from assignment rows (`TaskAssignment`, live ones only) in one grouped query per screen,
so a page costs the same for 5 people or 50. Order is always `assigned_at, id` (oldest first), never due
date or activity. Nothing here reads outside what the viewer may see: the two group kinds are bounded by
"assigned to me" and "assigned by me", and the wider "team" view goes through `Task.objects.visible_to`.
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings
from django.db.models import Count, Min, Q, QuerySet

from ots.accounts.models import Role, User
from ots.core.clock import now

from .models import (
    OPEN_STATUSES,
    AssignmentStatus,
    Priority,
    Task,
    TaskAssignment,
)

PAGE = 24
REVIEW_LIMIT = 50
S = AssignmentStatus
ACTIVE_STATUSES = (*OPEN_STATUSES, S.SUBMITTED)
EXCLUDED = (S.REMOVED, S.CANCELLED)

# filter chip -> the assignment statuses it shows. "overdue" is handled separately (it needs the due time).
FILTERS: dict[str, tuple[str, ...] | None] = {
    "open": ACTIVE_STATUSES,
    "all": None,
    "pending": (S.ASSIGNED,),
    "progress": (S.IN_PROGRESS,),
    "waiting": (S.BLOCKED, S.PAUSED),
    "changes": (S.CHANGES_REQUESTED,),
    "submitted": (S.SUBMITTED,),
    "completed": (S.APPROVED,),
    "overdue": OPEN_STATUSES,
}
DEFAULT_FILTER = "open"


@dataclass
class PersonCard:
    user: User
    total: int = 0
    active: int = 0
    completed: int = 0
    overdue: int = 0
    first_assigned: datetime.datetime | None = None
    unread_text: int = 0
    unread_voice: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def unread(self) -> int:
        return self.unread_text + self.unread_voice


def live_assignments() -> QuerySet[TaskAssignment]:
    """Rows that count: not removed, not cancelled, on a task that is not cancelled."""
    return TaskAssignment.objects.filter(removed_at__isnull=True, task__is_cancelled=False).exclude(
        status__in=EXCLUDED
    )


def _cards(qs: QuerySet[TaskAssignment], key: str) -> list[PersonCard]:
    """One grouped query for the counts and one for the people; ordered by who was assigned work first."""
    at = now()
    rows = list(
        qs.values(key)
        .annotate(
            total=Count("pk"),
            active=Count("pk", filter=Q(status__in=ACTIVE_STATUSES)),
            completed=Count("pk", filter=Q(status=S.APPROVED)),
            overdue=Count("pk", filter=Q(status__in=OPEN_STATUSES, task__due_at__lt=at)),
            first=Min("assigned_at"),
        )
        .order_by("first", key)
    )
    people = {u.pk: u for u in User.objects.filter(pk__in=[r[key] for r in rows])}
    return [
        PersonCard(
            user=people[r[key]],
            total=r["total"],
            active=r["active"],
            completed=r["completed"],
            overdue=r["overdue"],
            first_assigned=r["first"],
        )
        for r in rows
        if r[key] in people
    ]


def assigned_to_me_groups(viewer: User) -> list[PersonCard]:
    """One card per person who gave the viewer work."""
    return _cards(live_assignments().filter(user=viewer, assigned_by__isnull=False), "assigned_by_id")


def assigned_by_me_groups(viewer: User) -> list[PersonCard]:
    """One card per person the viewer gave work to."""
    return _cards(live_assignments().filter(assigned_by=viewer), "user_id")


def team_groups(viewer: User) -> list[PersonCard]:
    """Wider than 'assigned by me': every assignee of a task the viewer may see (Sub-Admin: their team's work,
    Super Admin: everyone). Employees and HR get nothing."""
    if viewer.role not in (Role.SUPER_ADMIN, Role.SUB_ADMIN):
        return []
    return _cards(live_assignments().filter(task__in=Task.objects.visible_to(viewer).values("pk")), "user_id")


def is_reviewer_q(viewer: User) -> Q:
    """Submitted work the viewer may approve: not their own, and they are the Super Admin, the task's creator,
    or the assignee's current manager (the `_is_reviewer` rule, as one filter)."""
    if viewer.role == Role.SUPER_ADMIN:
        return ~Q(user=viewer)
    if viewer.role == Role.SUB_ADMIN:
        return ~Q(user=viewer) & (Q(task__created_by=viewer) | Q(user__reports_to=viewer))
    return Q(pk__in=[])


def review_queue(viewer: User) -> list[TaskAssignment]:
    if viewer.role not in (Role.SUPER_ADMIN, Role.SUB_ADMIN):
        return []
    return list(
        live_assignments()
        .filter(status=S.SUBMITTED)
        .filter(is_reviewer_q(viewer))
        .select_related("task", "user")
        .order_by("submitted_at", "pk")[:REVIEW_LIMIT]
    )


# ---------------------------------------------------------------- a group page


@dataclass
class GroupPage:
    other: User
    rows: list[TaskAssignment]
    more_after: str  # keyset token for "Load more"; "" when this is the last page
    filter: str
    q: str
    total: int  # rows matching the filter (not just this page)
    counts: dict[str, int]  # per chip, for the labels


def other_user_or_none(raw: Any) -> User | None:
    try:
        return User.objects.filter(pk=uuid.UUID(str(raw))).first()
    except ValueError:
        return None


def _base(viewer: User, kind: str, other: User, team: bool) -> QuerySet[TaskAssignment]:
    if kind == "by":  # work the viewer holds, given by `other`
        return TaskAssignment.objects.filter(user=viewer, assigned_by=other)
    if team and viewer.role in (Role.SUPER_ADMIN, Role.SUB_ADMIN):  # the wider view, scoped by visible tasks
        return TaskAssignment.objects.filter(
            user=other, task__in=Task.objects.visible_to(viewer).values("pk")
        )
    return TaskAssignment.objects.filter(assigned_by=viewer, user=other)  # work the viewer gave to `other`


def has_relationship(viewer: User, kind: str, other: User, team: bool = False) -> bool:
    """True when the viewer and `other` share at least one assignment of this kind (removed ones count, so a
    card that existed yesterday still opens). Anything else is a 404: no existence leak."""
    return _base(viewer, kind, other, team).exists()


def _apply_filter(qs: QuerySet[TaskAssignment], chip: str, at: datetime.datetime) -> QuerySet[TaskAssignment]:
    statuses = FILTERS.get(chip, FILTERS[DEFAULT_FILTER])
    if chip == "all":
        return qs
    qs = qs.filter(status__in=statuses)
    if chip == "overdue":
        qs = qs.filter(task__due_at__lt=at)
    return qs


def _token(a: TaskAssignment) -> str:
    return f"{a.assigned_at.isoformat()}|{a.pk}"


def _after(raw: str) -> tuple[datetime.datetime, uuid.UUID] | None:
    try:
        when, pk = raw.split("|", 1)
        return datetime.datetime.fromisoformat(when), uuid.UUID(pk)
    except (ValueError, AttributeError):
        return None


def group_page(
    viewer: User, kind: str, other: User, *, chip: str = DEFAULT_FILTER, q: str = "", after: str = "",
    team: bool = False,
) -> GroupPage:  # fmt: skip
    if chip not in FILTERS:
        chip = DEFAULT_FILTER
    at = now()
    base = live_assignments().filter(pk__in=_base(viewer, kind, other, team).values("pk"))
    q = (q or "").replace("\x00", "").strip()[:100]
    if q:
        base = base.filter(Q(task__title__icontains=q) | Q(task__task_code__icontains=q))
    counts = _chip_counts(base, at)
    qs = (
        _apply_filter(base, chip, at)
        .select_related("task", "user", "assigned_by")
        .order_by("assigned_at", "pk")
    )
    cursor = _after(after)
    if cursor is not None:
        qs = qs.filter(Q(assigned_at__gt=cursor[0]) | Q(assigned_at=cursor[0], pk__gt=cursor[1]))
    rows = list(qs[: PAGE + 1])
    more = len(rows) > PAGE
    rows = rows[:PAGE]
    return GroupPage(
        other=other,
        rows=rows,
        more_after=_token(rows[-1]) if more and rows else "",
        filter=chip,
        q=q,
        total=counts.get(chip, 0),
        counts=counts,
    )


def _chip_counts(base: QuerySet[TaskAssignment], at: datetime.datetime) -> dict[str, int]:
    agg = base.aggregate(
        all=Count("pk"),
        open=Count("pk", filter=Q(status__in=ACTIVE_STATUSES)),
        pending=Count("pk", filter=Q(status=S.ASSIGNED)),
        progress=Count("pk", filter=Q(status=S.IN_PROGRESS)),
        waiting=Count("pk", filter=Q(status__in=(S.BLOCKED, S.PAUSED))),
        changes=Count("pk", filter=Q(status=S.CHANGES_REQUESTED)),
        submitted=Count("pk", filter=Q(status=S.SUBMITTED)),
        completed=Count("pk", filter=Q(status=S.APPROVED)),
        overdue=Count("pk", filter=Q(status__in=OPEN_STATUSES, task__due_at__lt=at)),
    )
    return {k: int(v) for k, v in agg.items()}


# ---------------------------------------------------------------- how a card looks


def accent(a: TaskAssignment, at: datetime.datetime | None = None) -> str:
    """Display-only urgency: overdue > due soon > urgent > high > normal > low. A finished assignment is muted
    and never overdue, and submitted work is waiting on a reviewer, not late."""
    at = at or now()
    if a.status == S.APPROVED:
        return "done"
    if a.status in OPEN_STATUSES and a.task.due_at < at:
        return "overdue"
    if a.status in OPEN_STATUSES and a.task.due_at - at <= datetime.timedelta(
        hours=settings.OTS_NEAR_DEADLINE_HOURS
    ):
        return "soon"
    return {
        Priority.URGENT: "urgent",
        Priority.HIGH: "high",
        Priority.NORMAL: "normal",
        Priority.LOW: "low",
    }.get(Priority(a.task.priority), "normal")


# ---------------------------------------------------------------- unread (derived, never stored)


def _unread_rows(viewer: User, task_ids: list[Any] | None = None) -> list[dict[str, Any]]:
    """The viewer's own unread message notifications, one row per (task, author, kind) with a count.
    Always filtered by `user=viewer`, so nobody else's read state or conversation leaks onto a card."""
    from ots.notifications.models import Kind, Notification

    qs = Notification.objects.filter(
        user=viewer, kind=Kind.COMMENT, read_at__isnull=True, comment__isnull=False
    )
    if task_ids is not None:
        qs = qs.filter(task_id__in=task_ids)
    fields = ["task_id", "comment__author_id"]
    if _has_comment_kind():
        fields.append("comment__kind")
    return list(qs.values(*fields).annotate(n=Count("pk")))


def _has_comment_kind() -> bool:
    from .models import TaskComment

    return any(f.name == "kind" for f in TaskComment._meta.get_fields())


def _bucket(row: dict[str, Any]) -> str:
    return "voice" if row.get("comment__kind") == "VOICE" else "text"


def _add(out: dict[Any, dict[str, int]], key: Any, row: dict[str, Any]) -> None:
    slot = out.setdefault(key, {"text": 0, "voice": 0})
    slot[_bucket(row)] += row["n"]


def unread_by_task(viewer: User, task_ids: list[Any]) -> dict[Any, dict[str, int]]:
    out: dict[Any, dict[str, int]] = {}
    for r in _unread_rows(viewer, task_ids):
        _add(out, r["task_id"], r)
    return out


def unread_by_assigner(viewer: User) -> dict[Any, dict[str, int]]:
    """Employee side: unread messages on tasks where the viewer holds work, counted on the card of whoever
    gave them that work."""
    rows = _unread_rows(viewer)
    if not rows:
        return {}
    given_by = dict(
        live_assignments()
        .filter(user=viewer, task_id__in={r["task_id"] for r in rows})
        .values_list("task_id", "assigned_by_id")
    )
    out: dict[Any, dict[str, int]] = {}
    for r in rows:
        who = given_by.get(r["task_id"])
        if who is not None:
            _add(out, who, r)
    return out


def unread_by_assignee(viewer: User) -> dict[Any, dict[str, int]]:
    """Manager side: unread messages written by a person the viewer gave work to, on the task the viewer gave
    them. A message by another of the viewer's assignees on a shared task counts on that author's own card."""
    rows = _unread_rows(viewer)
    if not rows:
        return {}
    mine = set(
        live_assignments()
        .filter(assigned_by=viewer, task_id__in={r["task_id"] for r in rows})
        .values_list("task_id", "user_id")
    )
    out: dict[Any, dict[str, int]] = {}
    for r in rows:
        if (r["task_id"], r["comment__author_id"]) in mine:
            _add(out, r["comment__author_id"], r)
    return out


def with_unread(cards: list[PersonCard], unread: dict[Any, dict[str, int]]) -> list[PersonCard]:
    for c in cards:
        u = unread.get(c.user.pk, {})
        c.unread_text, c.unread_voice = u.get("text", 0), u.get("voice", 0)
    return cards


# whole-card colour (IMP-15.7, D-100): the assignment's own state decides it, never priority or the due date
TONES = {
    S.ASSIGNED: "grey",
    S.IN_PROGRESS: "yellow",
    S.PAUSED: "yellow",
    S.BLOCKED: "red",
    S.CHANGES_REQUESTED: "pink",
    S.SUBMITTED: "blue",
    S.APPROVED: "green",
    S.CANCELLED: "muted",
    S.REMOVED: "muted",
}


def card_tone(a: TaskAssignment) -> str:
    """grey, yellow, red, pink, blue, green or muted. A cancelled task is muted whatever its people did."""
    if getattr(a.task, "is_cancelled", False):
        return "muted"
    return TONES.get(S(a.status), "grey")
