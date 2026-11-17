"""Office TV displays (M10 section 3): a short pairing code, then a long-lived token kept only as a hash."""

from __future__ import annotations

from django.db import models

from ots.accounts.models import User
from ots.core.clock import now
from ots.core.ids import uuid7


class DisplayPairingCode(models.Model):
    """A 6-digit code the Super Admin reads out to the TV. Valid for 10 minutes, used once. Stored hashed."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    code_hash = models.CharField(max_length=64)
    name = models.CharField(max_length=80)  # what the TV will be called ("Reception")
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(default=now)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "display_pairing_codes"
        indexes = [models.Index(fields=["code_hash", "expires_at"], name="pairing_lookup_idx")]

    def __str__(self) -> str:
        return f"pairing code for {self.name}"


class DisplayDevice(models.Model):
    """A paired TV. The token itself exists only in the TV's cookie; here is its SHA-256."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    name = models.CharField(max_length=80)
    token_hash = models.CharField(max_length=64, unique=True)
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(default=now)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT, related_name="+")

    class Meta:
        db_table = "display_devices"

    def __str__(self) -> str:
        return self.name
