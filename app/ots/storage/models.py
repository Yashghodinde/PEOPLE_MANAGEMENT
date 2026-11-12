from __future__ import annotations

import datetime

from django.db import models
from django.db.models.functions import Now

from ots.core.ids import uuid7

from . import crypto
from .provider import Library


class StorageOperation(models.Model):
    """One upload to SharePoint. Implements `OperationState`, so the provider saves its progress here."""

    class Kind(models.TextChoices):
        SHIP_WAL = "SHIP_WAL"  # priority 0: recovery data first
        UPLOAD_BACKUP_OBJECT = "UPLOAD_BACKUP_OBJECT"
        REPAIR_FILE = "REPAIR_FILE"
        ARCHIVE_FILE = "ARCHIVE_FILE"

    class State(models.TextChoices):
        PENDING = "PENDING"
        RUNNING = "RUNNING"
        RETRY_WAIT = "RETRY_WAIT"
        DONE = "DONE"
        FAILED = "FAILED"  # dead letter: needs a person

    class RemoteState(models.TextChoices):  # what the nightly reconciliation last saw in SharePoint
        OK = "OK"
        MISSING = "MISSING"  # deleted in SharePoint
        DRIFTED = "DRIFTED"  # content changed in SharePoint

    PRIORITY = {Kind.SHIP_WAL: 0, Kind.UPLOAD_BACKUP_OBJECT: 1, Kind.REPAIR_FILE: 2, Kind.ARCHIVE_FILE: 3}
    ACTIVE = (State.PENDING, State.RUNNING, State.RETRY_WAIT)

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    kind = models.CharField(max_length=30, choices=Kind.choices)
    state = models.CharField(max_length=12, choices=State.choices, default=State.PENDING)
    priority = models.PositiveSmallIntegerField()
    target_library = models.CharField(max_length=10, choices=[(x.value, x.value) for x in Library])
    local_path = models.CharField(max_length=500)
    remote_path = models.CharField(max_length=500)
    sha256 = models.CharField(max_length=64)
    size_bytes = models.BigIntegerField()
    ref_type = models.CharField(
        max_length=40, blank=True, default=""
    )  # what this upload is for: "blob", "wal" ...
    ref_id = models.CharField(max_length=64, blank=True, default="")
    attempts = models.PositiveIntegerField(default=0)  # failed tries so far
    first_attempt_at = models.DateTimeField(null=True, blank=True)  # the 24 h give-up clock starts here
    next_attempt_at = models.DateTimeField(db_default=Now())
    upload_url_enc = models.TextField(db_column="upload_url", blank=True, default="")  # encrypted
    upload_expires_at = models.DateTimeField(null=True, blank=True)
    confirmed_bytes = models.BigIntegerField(default=0)
    last_error = models.TextField(blank=True, default="")
    drive_id = models.CharField(max_length=200, blank=True, default="")
    item_id = models.CharField(max_length=200, blank=True, default="")
    etag = models.CharField(max_length=100, blank=True, default="")
    remote_hash = models.CharField(max_length=40, blank=True, default="")  # QuickXorHash verified at upload
    remote_state = models.CharField(max_length=10, choices=RemoteState.choices, default="OK")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "storage_operations"
        indexes = [
            models.Index(
                fields=["state", "next_attempt_at", "priority", "created_at"], name="storage_ops_claim_idx"
            ),
            models.Index(fields=["ref_type", "ref_id"], name="storage_ops_ref_idx"),
        ]
        constraints = [
            # the same thing is never queued twice while an earlier upload of it is still active
            models.UniqueConstraint(
                fields=["kind", "ref_type", "ref_id"],
                condition=models.Q(state__in=["PENDING", "RUNNING", "RETRY_WAIT"]) & ~models.Q(ref_id=""),
                name="storage_ops_one_active_per_ref",
            ),
            models.CheckConstraint(condition=models.Q(size_bytes__gte=0), name="storage_ops_size_ok"),
        ]

    def __str__(self) -> str:
        return f"{self.kind} {self.remote_path} [{self.state}]"

    # ---- OperationState (what the provider needs) -----------------------------------------------
    @property
    def upload_url(self) -> str:
        return crypto.decrypt(self.upload_url_enc)

    def save_session(self, upload_url: str, expires_at: datetime.datetime | None) -> None:
        self.upload_url_enc = crypto.encrypt(upload_url)
        self.upload_expires_at = expires_at
        self.save(update_fields=["upload_url_enc", "upload_expires_at", "updated_at"])

    def save_progress(self, confirmed_bytes: int) -> None:
        self.confirmed_bytes = confirmed_bytes
        self.save(update_fields=["confirmed_bytes", "updated_at"])

    def clear_session(self) -> None:
        self.upload_url_enc, self.upload_expires_at, self.confirmed_bytes = "", None, 0
        self.save(update_fields=["upload_url_enc", "upload_expires_at", "confirmed_bytes", "updated_at"])


class StorageCursor(models.Model):
    """Where the Graph delta feed of each library left off."""

    library = models.CharField(max_length=10, primary_key=True)
    delta_link = models.TextField()
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "storage_cursors"

    def __str__(self) -> str:
        return f"{self.library} delta cursor"


class StorageCapacity(models.Model):
    """One reading of the SharePoint quota per hour, so the growth can be charted and forecast."""

    id = models.BigAutoField(primary_key=True)
    at = models.DateTimeField()
    used_bytes = models.BigIntegerField()
    total_bytes = models.BigIntegerField()

    class Meta:
        db_table = "storage_capacity"
        indexes = [models.Index(fields=["-at"], name="storage_capacity_at_idx")]

    def __str__(self) -> str:
        return f"{self.at:%Y-%m-%d %H:%M} {self.used_bytes}/{self.total_bytes}"
