"""In-app notifications (M10 section 2). One row per person per update.

`read_at` is set when the person opens the task or the notification. Live push and the delivery ticks
(delivered / seen, D-052) arrive with M10; they will add their own column then.
"""

from __future__ import annotations

from django.db import models

from ots.accounts.models import User
from ots.core.clock import now
from ots.core.ids import uuid7
from ots.tasks.models import Task, TaskComment


class Kind(models.TextChoices):
    COMMENT = "COMMENT", "New message"
    ASSIGNED = "ASSIGNED", "Task assigned"
    SUBMITTED = "SUBMITTED", "Work submitted"
    CHANGES_REQUESTED = "CHANGES_REQUESTED", "Changes requested"
    APPROVED = "APPROVED", "Work approved"
    COMPLETED = "COMPLETED", "Task completed"
    BLOCKED = "BLOCKED", "Someone is waiting"
    CHANGED = "CHANGED", "Task changed"
    CANCELLED = "CANCELLED", "Task cancelled"
    REMOVED = "REMOVED", "Removed from task"
    FILE_READY = "FILE_READY", "File ready"
    FILE_HANDED = "FILE_HANDED", "File sent to you"


# What the little dot beside a task means (red = a message, green = finished, grey = anything else new).
DOT_CHAT = (Kind.COMMENT,)
DOT_DONE = (Kind.APPROVED, Kind.COMPLETED)


class Notification(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    kind = models.CharField(max_length=20, choices=Kind.choices)
    task = models.ForeignKey(Task, on_delete=models.PROTECT, related_name="+")
    comment = models.ForeignKey(
        TaskComment, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    title = models.CharField(max_length=200)
    body = models.CharField(max_length=200, blank=True, default="")
    created_at = models.DateTimeField(default=now)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "notifications"
        indexes = [
            models.Index(fields=["user", "read_at", "-created_at"], name="notif_user_unread_idx"),
            models.Index(fields=["task", "user"], name="notif_task_user_idx"),
            models.Index(fields=["comment"], name="notif_comment_idx"),
        ]

    def __str__(self) -> str:
        return self.title
