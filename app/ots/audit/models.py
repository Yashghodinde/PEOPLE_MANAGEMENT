from __future__ import annotations

from typing import Any

from django.db import models
from django.db.models.functions import Now


class AuditLogQuerySet(models.QuerySet["AuditLog"]):
    def visible_to(self, viewer: Any) -> AuditLogQuerySet:
        """Rows `viewer` may read (M02 table): Super Admin all; Sub-Admin records about their own team
        (as actor or as target) including themselves; Employee only their own login records."""
        from ots.accounts import selectors  # local import: accounts depends on audit, not the reverse
        from ots.accounts.models import Role

        if viewer is None or not getattr(viewer, "is_authenticated", False):
            return self.none()
        if viewer.role == Role.SUPER_ADMIN:
            return self
        if viewer.role == Role.SUB_ADMIN:
            ids = selectors.subtree_ids(viewer.pk) | {viewer.pk}
            about_them = models.Q(target_type="user", target_id__in=[str(i) for i in ids])
            return self.filter(models.Q(actor_id__in=ids) | about_them)
        mine = models.Q(actor_id=viewer.pk) | models.Q(target_type="user", target_id=str(viewer.pk))
        return self.filter(mine, action__startswith="auth.")


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

    objects = AuditLogQuerySet.as_manager()

    class Meta:
        db_table = "audit_log"
        indexes = [
            models.Index(fields=["actor_id", "at"], name="audit_actor_idx"),
            models.Index(fields=["action", "at"], name="audit_action_idx"),
            models.Index(fields=["target_type", "target_id"], name="audit_target_idx"),
            models.Index(
                fields=["-at", "-id"], name="audit_at_id_idx"
            ),  # the unfiltered viewer: newest first
        ]

    def __str__(self) -> str:
        return f"{self.action}#{self.id}"
