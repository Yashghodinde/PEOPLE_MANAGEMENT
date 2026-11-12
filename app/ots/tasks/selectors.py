"""Task reads: who may be given work, what a person still holds, and who a team held on a past date."""

from __future__ import annotations

import datetime
from typing import Any

from ots.accounts import selectors as people
from ots.accounts.models import Role, User
from ots.core.clock import IST

from .models import OPEN_STATUSES, TaskAssignment


def assignable_ids(actor: Any) -> set[Any]:
    """People `actor` may assign work to: everyone active for the Super Admin; their own team and themselves
    for a Sub-Admin; nobody for anyone else. The break-glass account never holds work."""
    if actor.role == Role.SUPER_ADMIN:
        return set(User.objects.filter(is_active=True, is_break_glass=False).values_list("pk", flat=True))
    if actor.role == Role.SUB_ADMIN:
        team = people.subtree_ids(actor.pk) | {actor.pk}
        return set(
            User.objects.filter(pk__in=team, is_active=True, is_break_glass=False).values_list(
                "pk", flat=True
            )
        )
    return set()


def assignable_users(actor: Any) -> list[User]:
    return list(User.objects.filter(pk__in=assignable_ids(actor)).order_by("full_name"))


def open_assignment_lines(user: Any) -> list[str]:
    """What this person still has to do, as "T-2026-000007 Monthly report" (blocks deactivation, F-TK-5)."""
    rows = (
        TaskAssignment.objects.filter(
            user_id=user.pk, removed_at__isnull=True, status__in=OPEN_STATUSES, task__is_cancelled=False
        )
        .select_related("task")
        .order_by("task__due_at")
    )
    return [f"{r.task.task_code} {r.task.title}" for r in rows]


def team_assignments_on(manager_id: Any, on: datetime.date) -> Any:
    """Assignments that existed on `on` for people who reported to `manager_id` THAT day (F-TK-6: a
    past-period report is attributed to whoever the manager was then, from reporting_history).
    The day runs from midnight to midnight in India time."""
    team = people.subtree_ids_at(manager_id, on)
    day_start = datetime.datetime.combine(on, datetime.time.min, IST)
    day_end = day_start + datetime.timedelta(days=1)
    return (
        TaskAssignment.objects.filter(user_id__in=team, assigned_at__lt=day_end)
        .exclude(removed_at__lt=day_start)  # taken off the task before that day began
        .select_related("task", "user")
    )
