"""Dashboard numbers (M07 section 6): one scoped set of queries per role, a fixed small number of them."""

from __future__ import annotations

import datetime
from typing import Any

from django.db.models import Count, Q

from ots.accounts import selectors as people
from ots.accounts.models import Role, User
from ots.core.clock import IST, now

from . import state
from .models import OPEN_STATUSES, AssignmentStatus, DerivedStatus, Task, TaskAssignment

LIST_LIMIT = 8


def _team(viewer: Any) -> set[Any] | None:
    """People whose assignments `viewer` oversees. None means everyone (Super Admin)."""
    if viewer.role == Role.SUPER_ADMIN:
        return None
    return people.subtree_ids(viewer.pk) | {viewer.pk}


def _scoped(viewer: Any) -> Any:
    qs = TaskAssignment.objects.exclude(status__in=(AssignmentStatus.REMOVED, AssignmentStatus.CANCELLED))
    team = _team(viewer)
    return qs if team is None else qs.filter(user_id__in=team)


def blocked_hours(a: TaskAssignment, at: datetime.datetime) -> float:
    """How long this person has been waiting, this time and before, in hours."""
    extra = (at - a.blocked_since).total_seconds() if a.blocked_since else 0
    return round((a.blocked_seconds + extra) / 3600, 1)


def manager_dashboard(viewer: Any) -> dict[str, Any]:
    """Super Admin (everything) and Sub-Admin (their team): the same numbers, different scope."""
    at = now()
    mine = _scoped(viewer)
    tasks = Task.objects.visible_to(viewer)
    by_status = dict(tasks.values_list("derived_status").annotate(n=Count("pk")).order_by())
    open_assignments = mine.filter(status__in=OPEN_STATUSES)
    day_start = datetime.datetime.combine(at.astimezone(IST).date(), datetime.time.min, IST)
    waiting = list(
        mine.filter(status=AssignmentStatus.BLOCKED).select_related("task", "user").order_by("blocked_since")[:LIST_LIMIT]
    )
    for a in waiting:
        a.hours_waiting = blocked_hours(a, at)  # type: ignore[attr-defined]
    workload = (
        open_assignments.values("user__full_name", "user_id")
        .annotate(open=Count("pk"), overdue=Count("pk", filter=Q(task__due_at__lt=at)))
        .order_by("-open", "user__full_name")[:LIST_LIMIT]
    )
    return {
        "counts": {s: by_status.get(s, 0) for s in DerivedStatus.values},
        "overdue": tasks.overdue(at).count(),
        "waiting": waiting,
        "recent_submissions": list(
            mine.filter(status=AssignmentStatus.SUBMITTED).select_related("task", "user").order_by("-submitted_at")[:LIST_LIMIT]
        ),
        "today": list(
            mine.filter(assigned_at__gte=day_start).select_related("task", "user").order_by("-assigned_at")[:LIST_LIMIT]
        ),
        "workload": list(workload),
    }


def employee_dashboard(viewer: User) -> dict[str, Any]:
    at = now()
    mine = TaskAssignment.objects.filter(user=viewer, removed_at__isnull=True).select_related("task")
    counts = dict(mine.values_list("status").annotate(n=Count("pk")).order_by())
    paused = list(mine.filter(status=AssignmentStatus.PAUSED).order_by("task__due_at"))
    soon = list(
        mine.filter(status__in=OPEN_STATUSES, task__due_at__lte=at + datetime.timedelta(hours=48), task__is_cancelled=False)
        .order_by("task__due_at")[: LIST_LIMIT + 4]
    )
    groups = [
        (label, counts.get(status, 0))
        for status, label in (
            (AssignmentStatus.ASSIGNED, state.employee_label(AssignmentStatus.ASSIGNED)),
            (AssignmentStatus.IN_PROGRESS, state.employee_label(AssignmentStatus.IN_PROGRESS)),
            (AssignmentStatus.PAUSED, state.employee_label(AssignmentStatus.PAUSED)),
            (AssignmentStatus.BLOCKED, "Waiting"),
            (AssignmentStatus.SUBMITTED, state.employee_label(AssignmentStatus.SUBMITTED)),
            (AssignmentStatus.CHANGES_REQUESTED, state.employee_label(AssignmentStatus.CHANGES_REQUESTED)),
            (AssignmentStatus.APPROVED, state.employee_label(AssignmentStatus.APPROVED)),
        )
    ]
    return {"paused": paused, "due_soon": soon, "groups": groups, "now": at}
