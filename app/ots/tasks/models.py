"""Tasks, per-person assignments, comments and the append-only event history (M07).

Status lives on the assignment (one per assignee); `Task.derived_status` is a cache recomputed in the same
transaction as every assignment change. Nothing here is ever deleted: foreign keys are PROTECT.
"""

from __future__ import annotations

import datetime
from typing import Any

from django.db import models
from django.db.models import Exists, OuterRef, Q

from ots.accounts.models import Company, User
from ots.core.clock import now
from ots.core.ids import uuid7


class Priority(models.TextChoices):
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    URGENT = "URGENT"


class AssignmentStatus(models.TextChoices):
    ASSIGNED = "ASSIGNED"
    IN_PROGRESS = "IN_PROGRESS"
    PAUSED = "PAUSED"
    BLOCKED = "BLOCKED"
    SUBMITTED = "SUBMITTED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    APPROVED = "APPROVED"
    REMOVED = "REMOVED"
    CANCELLED = "CANCELLED"


class DerivedStatus(models.TextChoices):
    ASSIGNED = "ASSIGNED"
    IN_PROGRESS = "IN_PROGRESS"
    AWAITING_REVIEW = "AWAITING_REVIEW"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class BlockReason(models.TextChoices):
    INSUFFICIENT_INPUT = "INSUFFICIENT_INPUT"
    DEPENDENCY = "DEPENDENCY"


class PauseReason(models.TextChoices):
    END_OF_DAY = "END_OF_DAY"


FINAL_STATUSES = (AssignmentStatus.APPROVED, AssignmentStatus.REMOVED, AssignmentStatus.CANCELLED)
# assignments somebody still has to act on: these are the ones that can be overdue
OPEN_STATUSES = (
    AssignmentStatus.ASSIGNED,
    AssignmentStatus.IN_PROGRESS,
    AssignmentStatus.PAUSED,
    AssignmentStatus.BLOCKED,
    AssignmentStatus.CHANGES_REQUESTED,
)


class TaskQuerySet(models.QuerySet["Task"]):
    def visible_to(self, viewer: Any) -> TaskQuerySet:
        """Tasks `viewer` may see: Super Admin all; Sub-Admin created by them or with an assignee in their
        team (or themselves); Employee those where they hold or held an assignment."""
        from ots.accounts import selectors
        from ots.accounts.models import Role

        if viewer is None or not getattr(viewer, "is_authenticated", False) or not viewer.is_active:
            return self.none()
        if viewer.role == Role.SUPER_ADMIN:
            return self
        if viewer.role == Role.SUB_ADMIN:
            team = selectors.subtree_ids(viewer.pk) | {viewer.pk}
            mine = Q(created_by_id=viewer.pk) | Exists(
                TaskAssignment.objects.filter(task=OuterRef("pk"), user_id__in=team)
            )
            return self.filter(mine)
        if viewer.role == Role.EMPLOYEE:
            return self.filter(Exists(TaskAssignment.objects.filter(task=OuterRef("pk"), user_id=viewer.pk)))
        return self.none()  # HR (no tasks at all) and any role not named above never fall through to Employee

    def overdue(self, at: datetime.datetime | None = None) -> TaskQuerySet:
        """Past its due time with an assignment still open. Overdue is never stored (M07 section 2)."""
        at = at or now()
        open_assignments = TaskAssignment.objects.filter(task=OuterRef("pk"), status__in=OPEN_STATUSES)
        return self.filter(due_at__lt=at, is_cancelled=False).filter(Exists(open_assignments))


class Task(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="tasks")
    task_code = models.CharField(max_length=20, unique=True)  # T-2026-000123
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")
    priority = models.CharField(max_length=10, choices=Priority.choices, default=Priority.NORMAL)
    category = models.CharField(max_length=60, blank=True, default="")
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="tasks_created")
    created_at = models.DateTimeField(default=now)
    scheduled_start_at = models.DateTimeField(null=True, blank=True)
    due_at = models.DateTimeField()
    derived_status = models.CharField(
        max_length=20, choices=DerivedStatus.choices, default=DerivedStatus.ASSIGNED
    )
    has_blocked = models.BooleanField(default=False)
    is_cancelled = models.BooleanField(default=False)
    hide_from_display = models.BooleanField(default=False)
    completed_at = models.DateTimeField(null=True, blank=True)
    version = models.PositiveIntegerField(default=1)
    # `search_vector` (a generated tsvector) lives only in the database: migration 0002 adds it with its
    # indexes. It is deliberately not a model field, so Django never tries to write a generated column.

    objects = TaskQuerySet.as_manager()

    class Meta:
        db_table = "tasks"
        indexes = [
            models.Index(fields=["derived_status", "due_at"], name="tasks_status_due_idx"),
            models.Index(fields=["created_by", "-created_at"], name="tasks_creator_idx"),
            models.Index(fields=["-created_at"], name="tasks_created_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.task_code} {self.title}"


class TaskAssignment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    task = models.ForeignKey(Task, on_delete=models.PROTECT, related_name="assignments")
    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="assignments")
    assigned_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    assigned_at = models.DateTimeField(default=now)
    status = models.CharField(
        max_length=20, choices=AssignmentStatus.choices, default=AssignmentStatus.ASSIGNED
    )
    pause_reason = models.CharField(max_length=20, choices=PauseReason.choices, blank=True, default="")
    block_reason = models.CharField(max_length=20, choices=BlockReason.choices, blank=True, default="")
    block_note = models.TextField(blank=True, default="")
    blocked_since = models.DateTimeField(null=True, blank=True)
    blocked_seconds = models.PositiveBigIntegerField(default=0)
    started_at = models.DateTimeField(null=True, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    submitted_late = models.BooleanField(default=False)
    approved_at = models.DateTimeField(null=True, blank=True)
    removed_at = models.DateTimeField(null=True, blank=True)
    version = models.PositiveIntegerField(default=1)

    class Meta:
        db_table = "task_assignments"
        constraints = [
            # one live assignment per person per task; a removed person can be added again later
            models.UniqueConstraint(
                fields=["task", "user"],
                condition=Q(removed_at__isnull=True),
                name="assignments_one_live_per_user",
            ),
        ]
        indexes = [
            models.Index(fields=["user", "status"], name="assign_user_status_idx"),
            models.Index(fields=["task", "status"], name="assign_task_status_idx"),
            # the card landing (IMP-09): who gave whom work, oldest first
            models.Index(fields=["assigned_by", "user"], name="assign_by_user_idx"),
            models.Index(fields=["user", "assigned_by", "assigned_at"], name="assign_user_by_at_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.task_id} -> {self.user_id} [{self.status}]"

    @property
    def is_final(self) -> bool:
        return self.status in FINAL_STATUSES


class TaskComment(models.Model):
    """Immutable (the table is append-only)."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    task = models.ForeignKey(Task, on_delete=models.PROTECT, related_name="comments")
    assignment = models.ForeignKey(
        TaskAssignment, null=True, blank=True, on_delete=models.PROTECT, related_name="comments"
    )
    author = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    body = models.TextField()
    created_at = models.DateTimeField(default=now)

    class Meta:
        db_table = "task_comments"
        indexes = [models.Index(fields=["task", "created_at"], name="comments_task_idx")]

    def __str__(self) -> str:
        return f"comment on {self.task_id} by {self.author_id}"


class TaskEvent(models.Model):
    """The task-domain history. Append-only: corrections are new rows."""

    id = models.BigAutoField(primary_key=True)
    task = models.ForeignKey(Task, on_delete=models.PROTECT, related_name="events")
    assignment = models.ForeignKey(
        TaskAssignment, null=True, blank=True, on_delete=models.PROTECT, related_name="events"
    )
    actor = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    event_type = models.CharField(max_length=60)
    payload: Any = models.JSONField(default=dict)
    created_at = models.DateTimeField(default=now)

    class Meta:
        db_table = "task_events"
        indexes = [
            models.Index(fields=["task", "id"], name="events_task_idx"),
            models.Index(fields=["actor", "created_at"], name="events_actor_idx"),
            models.Index(fields=["event_type", "created_at"], name="events_type_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.event_type}#{self.pk}"


class EventReceipt(models.Model):
    """Delivery receipt for a key task event (D-052): was the change saved, did it reach the person's screen,
    did they open the task. One row per recipient. It informs and never blocks or changes the task itself.
    Unlike task_events this row is updated (delivered_at, seen_at), which is why it is a separate table."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    event = models.ForeignKey(TaskEvent, on_delete=models.PROTECT, related_name="receipts")
    task = models.ForeignKey(Task, on_delete=models.PROTECT, related_name="receipts")
    assignment = models.ForeignKey(
        TaskAssignment, null=True, blank=True, on_delete=models.PROTECT, related_name="receipts"
    )
    sender = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    recipient = models.ForeignKey(User, on_delete=models.PROTECT, related_name="receipts")
    urgent = models.BooleanField(default=False)
    what = models.CharField(max_length=60)  # the event type, for the "not delivered yet" list
    created_at = models.DateTimeField(default=now)  # server time: when the change was saved
    delivered_at = models.DateTimeField(null=True, blank=True)  # server time of the recipient's ack
    seen_at = models.DateTimeField(null=True, blank=True)  # server time the recipient opened the task
    undelivered_notified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "event_receipts"
        constraints = [
            models.UniqueConstraint(fields=["event", "recipient"], name="receipt_event_recipient_uq")
        ]
        indexes = [
            models.Index(fields=["recipient", "delivered_at"], name="receipt_inbox_idx"),
            models.Index(fields=["task", "recipient", "-created_at"], name="receipt_task_idx"),
            models.Index(fields=["delivered_at", "created_at"], name="receipt_pending_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.what} -> {self.recipient_id}"
