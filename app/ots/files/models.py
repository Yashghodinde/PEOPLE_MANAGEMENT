"""Files (contract section 6, M08 section 1 to 8).

A *file* is the thing people talk about; a *version* is one upload of it; a *blob* is the bytes, stored once
by SHA-256 (the same content uploaded twice is kept once). Versions are never retried into existence: a retry
of the same Idempotency-Key returns the same version, a deliberate new upload is version N+1 (F-FL-7).
"""

from __future__ import annotations

from django.db import models

from ots.accounts.models import Company, User
from ots.core.clock import now
from ots.core.ids import uuid7
from ots.tasks.models import Task, TaskAssignment


class FileState(models.TextChoices):
    INITIATED = "INITIATED"
    RECEIVING = "RECEIVING"
    REJECTED = "REJECTED"
    ABANDONED = "ABANDONED"
    STAGED = "STAGED"
    ARCHIVING = "ARCHIVING"
    RETRY_WAIT = "RETRY_WAIT"
    BLOCKED_QUOTA = "BLOCKED_QUOTA"
    BLOCKED_AUTH = "BLOCKED_AUTH"
    VERIFYING = "VERIFYING"
    ARCHIVED = "ARCHIVED"
    FAILED = "FAILED"
    MISSING = "MISSING"
    DRIFTED = "DRIFTED"


class FileRelation(models.TextChoices):
    REFERENCE = "REFERENCE"
    SUBMISSION = "SUBMISSION"
    DELIVERABLE = "DELIVERABLE"
    VOICE = "VOICE"  # a recording in a task discussion (IMP-10); never listed in the Files panel


class FileStatus(models.TextChoices):
    ACTIVE = "ACTIVE"
    DELETED = "DELETED"  # hidden by its owner (personal files only); nothing is removed anywhere


class FileVisibility(models.TextChoices):
    PRIVATE = "PRIVATE"
    TASK = "TASK"
    LIBRARY = "LIBRARY"
    WORKSPACE = "WORKSPACE"  # uploaded into a File ID workspace: readable only through that link (IMP-04)


class LibraryState(models.TextChoices):
    APPROVED = "APPROVED", "Approved"  # also what every non-library file and every older library file is
    PENDING = "PENDING", "Waiting for approval"
    REJECTED = "REJECTED", "Not approved"


class LibraryFolder(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="children"
    )
    name = models.CharField(max_length=120)
    employee_upload = models.BooleanField(default=True)  # staff may put files here; each waits for approval

    class Meta:
        db_table = "library_folders"
        constraints = [
            models.UniqueConstraint(fields=["parent", "name"], name="library_folder_name_uq"),
            models.UniqueConstraint(
                fields=["name"], condition=models.Q(parent__isnull=True), name="library_root_name_uq"
            ),
        ]

    def __str__(self) -> str:
        return self.name


class FolderContext(models.TextChoices):
    PERSONAL = "PERSONAL"  # a person's own storage (IMP-14.2); the date level above the roots is virtual
    WORKSPACE = "WORKSPACE"  # inside a File ID


class Folder(models.Model):
    """A logical folder, independent of any SharePoint path. A file sits in at most one folder per context;
    the stored bytes never move. Personal roots belong to a date (`on_date`); deeper ones inherit it."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    context = models.CharField(max_length=10, choices=FolderContext.choices)
    owner = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    workspace_id = models.UUIDField(null=True, blank=True)
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="children"
    )
    name = models.CharField(max_length=120)
    name_key = models.CharField(max_length=120)  # lower-cased, so "GST" and "gst" are one name
    on_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=8, choices=FileStatus.choices, default=FileStatus.ACTIVE)
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(default=now)
    updated_at = models.DateTimeField(default=now)

    class Meta:
        db_table = "folders"
        constraints = [
            models.UniqueConstraint(
                fields=["context", "owner", "workspace_id", "parent", "on_date", "name_key"],
                name="folder_sibling_name_uq",
                nulls_distinct=False,
            ),
        ]
        indexes = [models.Index(fields=["context", "owner", "on_date"], name="folder_owner_day_ix")]

    def __str__(self) -> str:
        return self.name


class Blob(models.Model):
    """The bytes, keyed by SHA-256. `local_path` is empty once the cache evicted an ARCHIVED copy."""

    sha256 = models.CharField(max_length=64, primary_key=True)
    size_bytes = models.BigIntegerField()
    local_path = models.CharField(max_length=300, null=True, blank=True)  # noqa: DJ001 (null after eviction)
    cached = models.BooleanField(default=True)
    last_accessed_at = models.DateTimeField(default=now)
    drive_id = models.CharField(max_length=200, blank=True, default="")
    item_id = models.CharField(max_length=200, blank=True, default="")
    etag = models.CharField(max_length=200, blank=True, default="")
    ms_quick_xor_hash = models.CharField(max_length=60, blank=True, default="")
    archived_at = models.DateTimeField(null=True, blank=True)
    state = models.CharField(max_length=20, choices=FileState.choices, default=FileState.STAGED)

    class Meta:
        db_table = "blobs"
        constraints = [
            models.CheckConstraint(condition=models.Q(size_bytes__gte=0), name="blob_size_ck"),
            models.CheckConstraint(condition=models.Q(sha256__regex=r"^[0-9a-f]{64}$"), name="blob_sha_ck"),
        ]
        indexes = [models.Index(fields=["state", "last_accessed_at"], name="blob_evict_idx")]

    def __str__(self) -> str:
        return f"blob {self.sha256[:12]}"


class File(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="files")
    display_name = models.CharField(max_length=255)
    owner = models.ForeignKey(User, on_delete=models.PROTECT, related_name="files_owned")
    visibility = models.CharField(max_length=10, choices=FileVisibility.choices, default=FileVisibility.TASK)
    folder = models.ForeignKey(  # the personal folder a private file sits in (None = directly under its date)
        "Folder", null=True, blank=True, on_delete=models.PROTECT, related_name="files"
    )
    library_folder = models.ForeignKey(
        LibraryFolder, null=True, blank=True, on_delete=models.PROTECT, related_name="files"
    )
    current_version = models.ForeignKey(
        "FileVersion", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(default=now)
    status = models.CharField(max_length=8, choices=FileStatus.choices, default=FileStatus.ACTIVE)
    library_state = models.CharField(
        max_length=10, choices=LibraryState.choices, default=LibraryState.APPROVED
    )
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.PROTECT, related_name="files_hidden"
    )

    class Meta:
        db_table = "files"
        indexes = [
            models.Index(fields=["owner", "created_at"], name="files_owner_idx"),
            models.Index(fields=["owner", "status", "visibility"], name="files_owner_status_idx"),
        ]

    def __str__(self) -> str:
        return self.display_name


class FileVersion(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    file = models.ForeignKey(File, on_delete=models.PROTECT, related_name="versions")
    version_number = models.PositiveIntegerField()
    file_code = models.CharField(max_length=255, unique=True)
    original_filename = models.CharField(max_length=255)  # exactly what the browser sent; never edited
    display_name = models.CharField(
        max_length=255, blank=True, default=""
    )  # blank on files from before IMP-01
    uploaded_on = models.DateField()  # the Indian calendar date of `uploaded_at`; "Today" is built from it
    is_voice = models.BooleanField(
        default=False
    )  # a discussion voice note: only people who see the task hear it
    blob = models.ForeignKey(Blob, db_column="blob_sha256", on_delete=models.PROTECT, related_name="versions")
    mime_type = models.CharField(max_length=120, blank=True, default="")
    size_bytes = models.BigIntegerField()
    uploaded_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="file_versions")
    uploaded_at = models.DateTimeField(default=now)
    note = models.TextField(blank=True, default="")
    state = models.CharField(max_length=20, choices=FileState.choices, default=FileState.STAGED)
    idempotency_key = models.UUIDField(unique=True)

    class Meta:
        db_table = "file_versions"
        constraints = [
            models.UniqueConstraint(fields=["file", "version_number"], name="file_version_number_uq"),
            models.CheckConstraint(condition=models.Q(version_number__gte=1), name="file_version_ck"),
        ]
        indexes = [
            models.Index(fields=["uploaded_by", "uploaded_at"], name="fv_uploader_idx"),
            models.Index(fields=["state"], name="fv_state_idx"),
            models.Index(fields=["uploaded_by", "uploaded_on"], name="fv_uploader_day_idx"),
        ]

    def __str__(self) -> str:
        return self.file_code

    PREVIEW_TYPES = {
        "webm": "audio/webm",
        "m4a": "audio/mp4",
        "mp4": "audio/mp4",
        "ogg": "audio/ogg",
        "opus": "audio/ogg",
        "wav": "audio/wav",
        "mp3": "audio/mpeg",
        "pdf": "application/pdf",
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "webp": "image/webp",
        "txt": "text/plain; charset=utf-8",
    }

    @property
    def shown_name(self) -> str:
        """What a person sees: the name confirmed at upload, else the original (files from before IMP-01)."""
        return self.display_name or self.original_filename

    @property
    def extension(self) -> str:
        return self.file_code.rsplit(".", 1)[-1].lower() if "." in self.file_code else ""

    @property
    def preview_type(self) -> str | None:
        """The one content type a preview is served as (never what the file claims), or None."""
        return self.PREVIEW_TYPES.get(self.extension)


class UploadSession(models.Model):
    """One resumable browser upload. The same idempotency key always returns the same session (F-FL-1)."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    idempotency_key = models.UUIDField(unique=True)
    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="upload_sessions")
    task = models.ForeignKey(Task, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    assignment = models.ForeignKey(
        TaskAssignment, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    file = models.ForeignKey(File, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    relation = models.CharField(max_length=12, choices=FileRelation.choices, default=FileRelation.SUBMISSION)
    expected_size = models.BigIntegerField()
    expected_sha256 = models.CharField(max_length=64)
    original_filename = models.CharField(max_length=255)  # exactly what the browser sent; never edited
    display_name = models.CharField(
        max_length=255, blank=True, default=""
    )  # what the person confirmed (IMP-01)
    received_bytes = models.BigIntegerField(default=0)
    created_at = models.DateTimeField(default=now)
    last_piece_at = models.DateTimeField(default=now)
    state = models.CharField(max_length=20, choices=FileState.choices, default=FileState.RECEIVING)
    note = models.CharField(
        max_length=200, blank=True, default=""
    )  # an optional line the uploader adds (IMP-03)
    workspace_id = models.UUIDField(
        null=True, blank=True
    )  # the File ID workspace this goes into (scope "workspace")
    folder_id = models.UUIDField(null=True, blank=True)  # the personal folder the new file goes in (IMP-14.4)
    scope = models.CharField(max_length=10, blank=True, default="")  # "" = a task or the library; "personal"
    override_block = models.BooleanField(default=False)  # a Super Admin allowed a blocked type, audited

    class Meta:
        db_table = "upload_sessions"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(received_bytes__gte=0, received_bytes__lte=models.F("expected_size")),
                name="upload_received_ck",
            ),
        ]
        indexes = [models.Index(fields=["state", "last_piece_at"], name="upload_sweep_idx")]

    def __str__(self) -> str:
        return f"upload {self.original_filename} {self.received_bytes}/{self.expected_size}"


class TaskFile(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    task = models.ForeignKey(Task, on_delete=models.PROTECT, related_name="task_files")
    file = models.ForeignKey(File, on_delete=models.PROTECT, related_name="task_links")
    version = models.ForeignKey(FileVersion, on_delete=models.PROTECT, related_name="task_links")
    assignment = models.ForeignKey(
        TaskAssignment, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    relation = models.CharField(max_length=12, choices=FileRelation.choices)
    linked_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    linked_at = models.DateTimeField(default=now)

    class Meta:
        db_table = "task_files"
        constraints = [models.UniqueConstraint(fields=["task", "version", "relation"], name="task_file_uq")]
        indexes = [models.Index(fields=["task", "linked_at"], name="task_files_task_idx")]

    def __str__(self) -> str:
        return f"{self.task_id} -> {self.version_id}"


class FileHandover(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    version = models.ForeignKey(FileVersion, on_delete=models.PROTECT, related_name="handovers")
    from_user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="handovers_sent")
    task = models.ForeignKey(Task, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    note = models.TextField(blank=True, default="")
    sent_at = models.DateTimeField(default=now)

    class Meta:
        db_table = "file_handovers"
        indexes = [models.Index(fields=["version", "sent_at"], name="handover_version_idx")]

    def __str__(self) -> str:
        return f"handover of {self.version_id} by {self.from_user_id}"


class FileHandoverRecipient(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    handover = models.ForeignKey(FileHandover, on_delete=models.PROTECT, related_name="recipients")
    to_user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="handovers_received")
    first_opened_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "file_handover_recipients"
        constraints = [models.UniqueConstraint(fields=["handover", "to_user"], name="handover_recipient_uq")]
        indexes = [models.Index(fields=["to_user", "first_opened_at"], name="handover_inbox_idx")]

    def __str__(self) -> str:
        return f"{self.handover_id} to {self.to_user_id}"


class FileAccessLog(models.Model):
    """Who downloaded or previewed which version, and from where. Append-only."""

    class Action(models.TextChoices):
        DOWNLOAD = "DOWNLOAD"
        PREVIEW = "PREVIEW"

    class Source(models.TextChoices):
        CACHE = "CACHE"
        ONEDRIVE = "ONEDRIVE"

    id = models.BigAutoField(primary_key=True)
    version = models.ForeignKey(FileVersion, on_delete=models.PROTECT, related_name="access_log")
    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    action = models.CharField(max_length=10, choices=Action.choices)
    source = models.CharField(max_length=10, choices=Source.choices)
    at = models.DateTimeField(default=now)

    class Meta:
        db_table = "file_access_log"
        indexes = [
            models.Index(fields=["version", "id"], name="access_version_idx"),
            models.Index(fields=["user", "at"], name="access_user_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.action} {self.version_id} by {self.user_id}"


class FileFetchRequest(models.Model):
    """Someone asked for an archived file while SharePoint was out of reach (F-FL-6). The worker
    fetches it into the cache when it can, and tells them. One row per person per version."""

    class State(models.TextChoices):
        PENDING = "PENDING"
        DONE = "DONE"
        EXPIRED = "EXPIRED"

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    version = models.ForeignKey(FileVersion, on_delete=models.PROTECT, related_name="fetch_requests")
    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    state = models.CharField(max_length=10, choices=State.choices, default=State.PENDING)
    created_at = models.DateTimeField(default=now)
    done_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveIntegerField(
        default=0
    )  # failed fetches where the bytes arrived but did not verify
    next_attempt_at = models.DateTimeField(default=now)

    class Meta:
        db_table = "file_fetch_requests"
        constraints = [
            models.UniqueConstraint(fields=["version", "user"], name="fetch_request_uq"),
        ]
        indexes = [models.Index(fields=["state", "created_at"], name="fetch_pending_idx")]

    def __str__(self) -> str:
        return f"fetch {self.version_id} for {self.user_id} ({self.state})"


class LibraryReview(models.Model):
    """A manager's decision on a Library upload. Rows are never edited; a resubmission is a new upload."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    file = models.ForeignKey(File, on_delete=models.PROTECT, related_name="library_reviews")
    version = models.ForeignKey(FileVersion, on_delete=models.PROTECT, related_name="+")
    reviewer = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    decision = models.CharField(max_length=10, choices=LibraryState.choices)
    reason = models.CharField(max_length=300, blank=True, default="")
    at = models.DateTimeField(default=now)

    class Meta:
        db_table = "library_reviews"
        indexes = [models.Index(fields=["file", "at"], name="librev_file_ix")]

    def __str__(self) -> str:
        return f"{self.decision} {self.file_id}"
