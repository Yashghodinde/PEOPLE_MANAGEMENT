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


class PhysicalStatus(models.TextChoices):
    STORED = "STORED", "Stored"
    MISSING = "MISSING", "Missing"
    ARCHIVED = "ARCHIVED", "Archived"


class PhysicalRecord(models.Model):
    """A hard-copy record of a File ID and where it is kept (IMP-15.4). The number is a label, never a key to
    anything (D-102). Nothing is deleted: a record is marked Missing or Archived, and every change of place is
    a row in `PhysicalMove`."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    number = models.CharField(max_length=14, unique=True)  # PHY-000123
    workspace = models.ForeignKey(FileIdWorkspace, on_delete=models.PROTECT, related_name="physical_records")
    name = models.CharField(max_length=200)
    category = models.CharField(max_length=60, blank=True, default="")
    office = models.CharField(max_length=80)
    block = models.CharField(max_length=40)
    shelf = models.CharField(max_length=40)
    box = models.CharField(max_length=40, blank=True, default="")
    notes = models.TextField(blank=True, default="")
    status = models.CharField(max_length=10, choices=PhysicalStatus.choices, default=PhysicalStatus.STORED)
    stored_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    stored_at = models.DateTimeField(default=now)
    file = models.ForeignKey(
        File, null=True, blank=True, on_delete=models.PROTECT, related_name="physical_links"
    )

    class Meta:
        db_table = "physical_records"
        indexes = [models.Index(fields=["workspace", "status"], name="physrec_ws_status_ix")]

    def __str__(self) -> str:
        return self.number

    @property
    def place(self) -> str:
        parts = [self.office, f"Block {self.block}", f"Shelf {self.shelf}"] + (
            [f"Box {self.box}"] if self.box else []
        )
        return " > ".join(parts)


class PhysicalMove(models.Model):
    """Append-only: where a record was kept before and where it went. The first row is the initial storage."""

    id = models.BigAutoField(primary_key=True)
    record = models.ForeignKey(PhysicalRecord, on_delete=models.PROTECT, related_name="moves")
    from_office = models.CharField(max_length=80, blank=True, default="")
    from_block = models.CharField(max_length=40, blank=True, default="")
    from_shelf = models.CharField(max_length=40, blank=True, default="")
    from_box = models.CharField(max_length=40, blank=True, default="")
    to_office = models.CharField(max_length=80)
    to_block = models.CharField(max_length=40)
    to_shelf = models.CharField(max_length=40)
    to_box = models.CharField(max_length=40, blank=True, default="")
    changed_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    changed_at = models.DateTimeField(default=now)
    reason = models.CharField(max_length=200, blank=True, default="")

    class Meta:
        db_table = "physical_moves"
        indexes = [models.Index(fields=["record", "changed_at"], name="physmove_record_ix")]

    def __str__(self) -> str:
        return f"{self.record_id}: {self.to_office}/{self.to_block}/{self.to_shelf}"
