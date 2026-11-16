"""Deadline reminders (M10 section 2). Run by the worker; safe to re-run (unique per window)."""

from __future__ import annotations

import datetime
from typing import Any

from ots.core.clock import IST, now
from ots.core.jobs import job
from ots.tasks.models import OPEN_STATUSES, TaskAssignment

from . import services
from .models import Kind

FIRST, LAST = datetime.time(8, 0), datetime.time(20, 0)  # reminders go out from 08:00 to 20:00 IST
OVERDUE_AT = datetime.time(10, 0)  # overdue work is mentioned once a day, from 10:00
WINDOWS = (("2h", datetime.timedelta(hours=2)), ("24h", datetime.timedelta(hours=24)))
BATCH = 500


def _open_assignments(due_before: datetime.datetime) -> Any:
    return (
        TaskAssignment.objects.filter(
            status__in=OPEN_STATUSES,
            removed_at__isnull=True,
            task__is_cancelled=False,
            task__due_at__lt=due_before,
        )
        .select_related("task", "user")
        .order_by("task__due_at")
    )


def run(at: datetime.datetime | None = None) -> int:
    """Send what is due. Returns how many reminders were created."""
    at = at or now()
    local = at.astimezone(IST)
    if not FIRST <= local.time() <= LAST:
        return 0
    made = 0
    # Coming up: the tightest window that applies, once per assignment and window.
    for a in _open_assignments(at + WINDOWS[-1][1]).filter(task__due_at__gte=at)[:BATCH]:
        left = a.task.due_at - at
        window = next(name for name, span in WINDOWS if left <= span)
        due = a.task.due_at.astimezone(IST)
        when = "in under 2 hours" if window == "2h" else "within 24 hours"
        made += len(
            services.notify(
                [a.user_id], Kind.REMINDER, a.task, f"Due {when}: {a.task.title}",
                body=f"{a.task.task_code}, due {due:%d %b %H:%M}", source=f"reminder:{a.pk}:{window}",
            )
        )  # fmt: skip
    # Overdue: one a day, from 10:00.
    if local.time() >= OVERDUE_AT:
        for a in _open_assignments(at)[:BATCH]:
            due = a.task.due_at.astimezone(IST)
            made += len(
                services.notify(
                    [a.user_id], Kind.REMINDER, a.task, f"Overdue: {a.task.title}",
                    body=f"{a.task.task_code} was due {due:%d %b %H:%M}",
                    source=f"reminder:{a.pk}:overdue:{local.date().isoformat()}",
                )
            )  # fmt: skip
    return made


@job("notifications.deadline_reminders", schedule="*/15 8-20 * * *")
def deadline_reminders() -> None:
    run()
