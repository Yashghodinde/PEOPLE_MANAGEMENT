"""Backup bookkeeping (M04): runs, the objects they put in SharePoint, and restore tests."""

from __future__ import annotations

from django.db import models

from ots.core.clock import now
from ots.core.ids import uuid7


class BackupRun(models.Model):
    class Type(models.TextChoices):
        BASE = "BASE"
        LOGICAL = "LOGICAL"

    class Status(models.TextChoices):
        RUNNING = "RUNNING"
        SUCCESS = "SUCCESS"
        PARTIAL = "PARTIAL"  # everything is uploaded but a check found a difference
        FAILED = "FAILED"

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    type = models.CharField(max_length=10, choices=Type.choices)
    started_at = models.DateTimeField(default=now)
    finished_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.RUNNING)
    step = models.CharField(max_length=30, blank=True, default="")  # where it stopped, when it did
    detail = models.JSONField(default=dict)
    size_bytes = models.BigIntegerField(default=0)

    class Meta:
        db_table = "backup_runs"
        indexes = [models.Index(fields=["type", "-started_at"], name="backup_runs_type_idx")]

    def __str__(self) -> str:
        return f"{self.type} {self.status} {self.started_at:%Y-%m-%d %H:%M}"


class BackupObject(models.Model):
    """One file that is (or is about to be) in SharePoint's DR library. `confirmed_at` is set only once the
    upload was verified there; nothing is counted as protected before that."""

    class Type(models.TextChoices):
        WAL = "WAL"
        BASE = "BASE"
        LOGICAL = "LOGICAL"  # one artefact of a nightly set

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    run = models.ForeignKey(
        BackupRun, null=True, blank=True, on_delete=models.PROTECT, related_name="stored_objects"
    )
    type = models.CharField(max_length=10, choices=Type.choices)
    name = models.CharField(max_length=200)
    remote_path = models.CharField(max_length=400, unique=True)
    sha256 = models.CharField(max_length=64)  # of the encrypted file that was uploaded
    size_bytes = models.BigIntegerField()
    wal_segment = models.CharField(max_length=40, blank=True, default="")
    wal_timeline = models.PositiveIntegerField(null=True, blank=True)
    wal_end_lsn = models.BigIntegerField(null=True, blank=True)  # byte position where this segment ends
    wal_end_time = models.DateTimeField(null=True, blank=True)  # when it was archived (see D-074)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    pruned_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "backup_objects"
        indexes = [
            models.Index(fields=["type", "wal_timeline", "wal_segment"], name="backup_obj_wal_idx"),
            models.Index(fields=["type", "confirmed_at"], name="backup_obj_confirmed_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.type} {self.name}"


class RecoveryTest(models.Model):
    class Type(models.TextChoices):
        LOGICAL = "LOGICAL"
        PITR = "PITR"
        KEY = "KEY"

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    type = models.CharField(max_length=10, choices=Type.choices)
    started_at = models.DateTimeField(default=now)
    finished_at = models.DateTimeField(null=True, blank=True)
    passed = models.BooleanField(null=True, blank=True)  # None while it is still running
    detail = models.JSONField(default=dict)

    class Meta:
        db_table = "recovery_tests"
        indexes = [models.Index(fields=["type", "-started_at"], name="recovery_tests_idx")]

    def __str__(self) -> str:
        return f"{self.type} test {self.started_at:%Y-%m-%d}"
