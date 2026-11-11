from django.db import models
from django.db.models.functions import Now


class AuditLog(models.Model):
    """Security and administration history. Append-only (DB trigger + revoked privileges)."""

    id = models.BigAutoField(primary_key=True)
    at = models.DateTimeField(db_default=Now())
    actor_id = models.UUIDField(null=True, blank=True)
    action = models.CharField(max_length=80)
    target_type = models.CharField(max_length=60, blank=True, default="")
    target_id = models.CharField(max_length=64, blank=True, default="")
    payload = models.JSONField(default=dict)
    client_ip = models.GenericIPAddressField(null=True, blank=True)
    request_id = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        db_table = "audit_log"
        indexes = [
            models.Index(fields=["actor_id", "at"], name="audit_actor_idx"),
            models.Index(fields=["action", "at"], name="audit_action_idx"),
            models.Index(fields=["target_type", "target_id"], name="audit_target_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.action}#{self.id}"
