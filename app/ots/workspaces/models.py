"""File ID workspaces (IMP-04): a shared client, company or project space with its own unique File ID, members
with roles, and linked documents. Rows are never deleted: a removal or a revocation sets a timestamp."""

from __future__ import annotations

from django.contrib.postgres.fields import ArrayField
from django.db import models

from ots.accounts.models import User
from ots.core.clock import now
from ots.core.ids import uuid7
from ots.files.models import File


class Category(models.TextChoices):
    CLIENT = "CLIENT", "Client"
    COMPANY = "COMPANY", "Company"
    PROJECT = "PROJECT", "Project"
    INTERNAL = "INTERNAL", "Internal"
    OTHER = "OTHER", "Other"


class WorkspaceStatus(models.TextChoices):
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


class Role(models.TextChoices):
    OWNER = "OWNER", "Owner"
    CONTRIBUTOR = "CONTRIBUTOR", "Contributor"
    VIEWER = "VIEWER", "Viewer"


class FileIdWorkspace(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    file_id = models.CharField(max_length=40)  # the code as it is shown
    file_id_norm = models.CharField(max_length=40, unique=True)  # upper-cased: what uniqueness is decided on
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")
    category = models.CharField(max_length=10, choices=Category.choices, default=Category.CLIENT)
    tags = ArrayField(models.CharField(max_length=40), default=list, blank=True)
    owner = models.ForeignKey(User, on_delete=models.PROTECT, related_name="workspaces_owned")
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="workspaces_created")
    created_at = models.DateTimeField(default=now)
    status = models.CharField(max_length=8, choices=WorkspaceStatus.choices, default=WorkspaceStatus.ACTIVE)
    physical_visible_to_viewers = models.BooleanField(default=True)

    class Meta:
        db_table = "file_id_workspaces"
        indexes = [models.Index(fields=["status", "created_at"], name="fidws_status_idx")]

    def __str__(self) -> str:
        return self.file_id


class FileIdMember(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    workspace = models.ForeignKey(FileIdWorkspace, on_delete=models.PROTECT, related_name="members")
    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="workspace_memberships")
    role = models.CharField(max_length=12, choices=Role.choices)
    added_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    added_at = models.DateTimeField(default=now)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT, related_name="+")

    class Meta:
        db_table = "file_id_members"
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "user"],
                condition=models.Q(revoked_at__isnull=True),
                name="fidmember_one_active_uq",
            )
        ]
        indexes = [models.Index(fields=["user", "revoked_at"], name="fidmember_user_idx")]

    def __str__(self) -> str:
        return f"{self.user_id} in {self.workspace_id} as {self.role}"


class FileIdDocument(models.Model):
    """A link from a workspace to an existing File. Nothing is copied."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    workspace = models.ForeignKey(FileIdWorkspace, on_delete=models.PROTECT, related_name="documents")
    file = models.ForeignKey(File, on_delete=models.PROTECT, related_name="workspace_links")
    added_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    added_at = models.DateTimeField(default=now)
    removed_at = models.DateTimeField(null=True, blank=True)
    removed_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    folder = models.ForeignKey(  # the workspace folder it sits in (None = at the top of the File ID)
        "files.Folder", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        db_table = "file_id_documents"
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "file"],
                condition=models.Q(removed_at__isnull=True),
                name="fiddoc_one_active_uq",
            )
        ]
        indexes = [models.Index(fields=["file", "removed_at"], name="fiddoc_file_idx")]

    def __str__(self) -> str:
        return f"{self.file_id} in {self.workspace_id}"
