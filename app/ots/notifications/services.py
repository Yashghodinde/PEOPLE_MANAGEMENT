"""Writing and clearing notifications. All changes happen inside the caller's transaction."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any

from ots.core.clock import now

from .models import Kind, Notification


def notify(
    users: Iterable[Any],
    kind: str,
    task: Any,
    title: str,
    *,
    actor: Any = None,
    comment: Any = None,
    body: str = "",
    source: str = "",
    link: str = "",
) -> list[Notification]:
    """One notification per person, never to the person who did it, never twice for the same cause.

    `source` names the cause (an outbox event id, or a reminder window). A repeat with the same source
    for the same person and kind is dropped, even from two handlers racing (F-NT-1).
    """
    skip = {actor.pk} if actor is not None else set()
    wanted = {u for u in users if u is not None and u not in skip}
    if comment is not None:  # a retried handler must not double up (F-NT-1)
        wanted -= set(Notification.objects.filter(comment=comment).values_list("user_id", flat=True))
    if source:
        wanted -= set(Notification.objects.filter(kind=kind, source=source).values_list("user_id", flat=True))
    made = [
        Notification(
            user_id=u,
            kind=kind,
            task=task,
            comment=comment,
            title=title[:200],
            body=body[:200],
            source=source,
            link=link[:200],
        )
        for u in wanted
    ]
    Notification.objects.bulk_create(made, ignore_conflicts=bool(source))
    if made:  # the bell of each person updates by itself (delivered when the transaction commits)
        from ots.live.publisher import send

        send(users=[n.user_id for n in made], type_="notification")
    return made


def mark_task_read(user: Any, task_id: Any) -> int:
    """Opening a task clears its dot and its bell entries."""
    return Notification.objects.filter(user=user, task_id=task_id, read_at__isnull=True).update(read_at=now())


def mark_all_read(user: Any) -> int:
    return Notification.objects.filter(user=user, read_at__isnull=True).update(read_at=now())


def open_one(user: Any, notification_id: Any) -> Notification | None:
    try:
        pk = uuid.UUID(str(notification_id))
    except ValueError:
        return None
    n = Notification.objects.filter(pk=pk, user=user).select_related("task").first()
    if n is not None and n.read_at is None:
        n.read_at = now()
        n.save(update_fields=["read_at"])
    return n


__all__ = ["Kind", "mark_all_read", "mark_task_read", "notify", "open_one"]
