"""Attendance (M06): work sessions, daily roll-up, corrections, holidays. Server times only."""

from __future__ import annotations

from django.db import models

from ots.accounts.models import Company, User
from ots.core.clock import now
from ots.core.ids import uuid7


class IdleMode(models.TextChoices):
    OS = "OS", "Whole computer"
    TAB_ONLY = "TAB_ONLY", "This tab only"


class SessionEndReason(models.TextChoices):
    END_OF_DAY = "END_OF_DAY", "End of day"
    MANUAL = "MANUAL", "Logged out (break)"
    IDLE_45 = "IDLE_45", "Idle for 45 minutes"
    BROWSER_CLOSED = "BROWSER_CLOSED", "Browser closed"
    FORCED = "FORCED", "Ended by an administrator"


class AttendanceStatus(models.TextChoices):
    P = "P", "Present"
    L = "L", "Late"
    A = "A", "Absent"
    WO = "WO", "Weekly off"
    H = "H", "Holiday"


class WorkSession(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="work_sessions")
    work_date = models.DateField()
    login_at = models.DateTimeField(default=now)
    selfie_path = models.CharField(max_length=300)
    idle_mode = models.CharField(max_length=10, choices=IdleMode.choices, default=IdleMode.TAB_ONLY)
    idle_started_at = models.DateTimeField(null=True, blank=True)
    active_resumed_at = models.DateTimeField(null=True, blank=True)
    idle_seconds_total = models.PositiveIntegerField(default=0)
    live_connected = models.BooleanField(default=False)
    live_disconnected_at = models.DateTimeField(null=True, blank=True)
    logout_at = models.DateTimeField(null=True, blank=True)
    logout_reason = models.CharField(max_length=15, choices=SessionEndReason.choices, blank=True, default="")
    active_seconds = models.PositiveIntegerField(null=True, blank=True)
    client_ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=300, blank=True, default="")
    device_id = models.CharField(max_length=64, blank=True, default="")
    # Added to the contract's columns (D-059): which Django session this is, so the server can end it; and the
    # client's idempotency key, so a repeated submission finds the session it already made (F-AT-6).
    session_key = models.CharField(max_length=40, blank=True, default="")
    login_key = models.UUIDField(null=True, blank=True, unique=True)

    class Meta:
        db_table = "work_sessions"
        indexes = [
            models.Index(fields=["user", "work_date"], name="ws_user_date_idx"),
            models.Index(
                fields=["logout_at"], name="ws_open_idx", condition=models.Q(logout_at__isnull=True)
            ),
        ]

    def __str__(self) -> str:
        return f"{self.user_id} {self.work_date}"


class AttendanceDay(models.Model):
    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="attendance_days")
    date = models.DateField()
    status = models.CharField(max_length=2, choices=AttendanceStatus.choices)
    first_login = models.DateTimeField(null=True, blank=True)
    last_logout = models.DateTimeField(null=True, blank=True)
    active_minutes = models.PositiveIntegerField(default=0)
    sessions = models.PositiveIntegerField(default=0)
    idle_logouts = models.PositiveIntegerField(default=0)
    late_minutes = models.PositiveIntegerField(default=0)
    computed_at = models.DateTimeField(default=now)

    class Meta:
        db_table = "attendance_days"
        constraints = [models.UniqueConstraint(fields=["user", "date"], name="attendance_day_pk")]

    def __str__(self) -> str:
        return f"{self.user_id} {self.date} {self.status}"


class AttendanceCorrection(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="attendance_corrections")
    date = models.DateField()
    new_status = models.CharField(max_length=2, choices=AttendanceStatus.choices)
    reason = models.TextField()
    requested_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    approved_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    approved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=now)

    class Meta:
        db_table = "attendance_corrections"
        indexes = [models.Index(fields=["user", "date"], name="att_corr_user_date_idx")]

    def __str__(self) -> str:
        return f"{self.user_id} {self.date} {self.new_status}"


class Holiday(models.Model):
    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="holidays")
    date = models.DateField()
    name = models.CharField(max_length=100)

    class Meta:
        db_table = "holidays"
        constraints = [models.UniqueConstraint(fields=["company", "date"], name="holiday_company_date")]

    def __str__(self) -> str:
        return f"{self.date} {self.name}"
