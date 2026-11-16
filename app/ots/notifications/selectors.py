"""Reads for the bell, the dots and the message ticks."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from .models import DOT_CHAT, DOT_DONE, Notification

BELL_LIMIT = 30


def unread_count(user: Any) -> int:
    if user is None or not getattr(user, "is_authenticated", False):
        return 0
    return Notification.objects.filter(user=user, read_at__isnull=True).count()


def bell_items(user: Any) -> list[Notification]:
    return list(
        Notification.objects.filter(user=user).select_related("task").order_by("-created_at")[:BELL_LIMIT]
    )


def dots_for(user: Any, task_ids: Iterable[Any] | None = None) -> dict[str, str]:
    """{task id: 'chat' | 'done' | 'update'} for tasks with something unread. Chat wins, then done."""
    qs = Notification.objects.filter(user=user, read_at__isnull=True, task__isnull=False)
    if task_ids is not None:
        qs = qs.filter(task_id__in=list(task_ids))
    kinds: dict[str, set[str]] = defaultdict(set)
    for task_id, kind in qs.values_list("task_id", "kind").distinct():
        kinds[str(task_id)].add(kind)
    out = {}
    for tid, ks in kinds.items():
        out[tid] = "chat" if ks & set(DOT_CHAT) else "done" if ks & set(DOT_DONE) else "update"
    return out


def unread_tasks(user: Any, limit: int = 8) -> list[Any]:
    """Tasks with something new for this person, newest first (the dashboard's 'New for you')."""
    rows = (
        Notification.objects.filter(user=user, read_at__isnull=True, task__isnull=False)
        .select_related("task")
        .order_by("-created_at")[:50]
    )
    seen: dict[Any, Any] = {}
    for n in rows:
        seen.setdefault(n.task_id, n.task)
    return list(seen.values())[:limit]
