from django.db import models
from django.db.models.functions import Now


class IdempotencyRecord(models.Model):
    """Stored response for an Idempotency-Key. Purged after 7 days (sync.purge_idempotency)."""

    key = models.UUIDField(primary_key=True)
    user_id = models.UUIDField(null=True, blank=True)
    method = models.CharField(max_length=8)
    path = models.TextField()
    status_code = models.PositiveSmallIntegerField()
    content_type = models.CharField(max_length=100, default="application/json")
    response_body = models.BinaryField()  # gzip
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "idempotency_records"
        indexes = [models.Index(fields=["created_at"], name="idem_created_idx")]

    def __str__(self) -> str:
        return f"{self.method} {self.path} [{self.key}]"


class OutboxEvent(models.Model):
    """Append-only event log; only published_at/payload (dead-letter marker) change, via DB functions."""

    id = models.BigAutoField(primary_key=True)
    at = models.DateTimeField(db_default=Now())
    type = models.CharField(max_length=100)
    aggregate_type = models.CharField(max_length=60)
    aggregate_id = models.CharField(max_length=64)
    payload = models.JSONField(default=dict)
    published_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "outbox_events"
        indexes = [
            models.Index(
                fields=["id"], name="outbox_unpublished_idx", condition=models.Q(published_at__isnull=True)
            )
        ]

    def __str__(self) -> str:
        return f"{self.type}#{self.id}"
