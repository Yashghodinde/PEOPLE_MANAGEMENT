from django.db import models


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
