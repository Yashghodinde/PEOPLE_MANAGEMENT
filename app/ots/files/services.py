"""Upload pipeline writes (M08 section 2): start, receive pieces, commit, cancel, sweep.

The bytes go to `staging/{id}.part` piece by piece, and only the last piece commits: one transaction makes
the blob, the file and its version, the task link, the task event, the audit row, the archive operation and
the outbox events. A repeated Idempotency-Key never makes a second version (F-FL-7).
"""

from __future__ import annotations

import datetime
import hashlib
import logging
import mimetypes
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Max

from ots.accounts.models import Role, User
from ots.audit import api as audit
from ots.core import events
from ots.core.clock import IST, now, today_local
from ots.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from ots.core.permissions import require
from ots.storage import manifest, queue
from ots.storage.models import StorageOperation as Op
from ots.storage.provider import Library
from ots.tasks.models import OPEN_STATUSES, Task, TaskAssignment, TaskEvent

from . import cache, layout, naming
from .blocklist import is_blocked
from .codes import make_code, unique_code
from .errors import HashMismatch, OffsetMismatch
from .folders import area_of_id
from .models import (
    Blob,
    File,
    FileArea,
    FileRelation,
    FileState,
    FileStatus,
    FileVersion,
    FileVisibility,
    LibraryState,
    TaskFile,
    UploadSession,
)

CHUNK = 1024 * 1024  # never hold more than this in memory
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
ABANDON_AFTER = datetime.timedelta(hours=24)


@dataclass(frozen=True)
class Progress:
    session: UploadSession
    version: FileVersion | None  # set once the last piece has committed


# --- start ----------------------------------------------------------------------------------------


AUDIO_EXTENSIONS = {"webm", "m4a", "mp4", "ogg", "opus", "wav", "mp3"}


def _voice_name(task: Task | None, original: str) -> str:
    """`voice-<task code>-<yyyymmdd-HHMMSS>.<ext>` in Indian time. Refuses anything that is not audio."""
    ext = original.rsplit(".", 1)[-1].lower() if "." in original else ""
    if ext not in AUDIO_EXTENSIONS:
        raise ValidationFailed(
            "A voice message must be an audio recording.", fields={"original_filename": ["Not audio."]}
        )
    if task is None:
        raise ValidationFailed("A voice message belongs to a task.", fields={"task_id": ["Required."]})
    return f"voice-{task.task_code}-{now().astimezone(IST):%Y%m%d-%H%M%S}.{ext}"


def _workspace_token(s: UploadSession) -> str | None:
    """The File ID in the code of a file uploaded into a workspace, so the code says where it belongs."""
    if s.scope != WORKSPACE_SCOPE or not s.workspace_id:
        return None
    from ots.workspaces.models import FileIdWorkspace

    return FileIdWorkspace.objects.filter(pk=s.workspace_id).values_list("file_id", flat=True).first()


def _clean_name(raw: str, field: str = "original_filename") -> str:
    name = (raw or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not name or name in {".", ".."} or len(name) > 255 or "\x00" in name:
        raise ValidationFailed("That file name is not valid.", fields={field: ["Invalid."]})
    return name


VOICE_PURPOSE = "voice"  # set by the server for voice notes (IMP-10); a client cannot ask for it


def _require_meaningful_name(name: str, task: Task | None) -> None:
    ctx = naming.NameContext(
        task_code=task.task_code if task else "", task_title=task.title if task else "", today=today_local()
    )
    verdict = naming.judge(name, ctx)
    if not verdict.ok:
        raise ValidationFailed(
            verdict.message, fields={"display_name": [verdict.message]}, suggestion=verdict.suggestion
        )


PERSONAL = "personal"  # an upload into the sender's own private storage (IMP-02)
WORKSPACE_SCOPE = "workspace"  # an upload into a File ID workspace (IMP-04)
CHAT_SCOPE = "chat"  # a file sent in a conversation (IMP-16.11)
LIBRARY_SCOPE = "library"  # an upload into a Library folder (IMP-14); who may is the usual library rule


def _resolve_workspace_target(user: User, workspace: Any, file_id: uuid.UUID | None) -> File | None:
    """A Contributor or Owner (or the Super Admin) uploads into the workspace. A new version may only be added
    to a document that is linked there now."""
    require(user, "fileid.contribute", workspace)
    if not file_id:
        return None
    from ots.workspaces.models import FileIdDocument

    linked = FileIdDocument.objects.filter(
        workspace=workspace, file_id=file_id, removed_at__isnull=True
    ).exists()
    existing = File.objects.filter(pk=file_id).first() if linked else None
    if existing is None:
        raise ValidationFailed("That document is not in this File ID.", fields={"file_id": ["Invalid."]})
    return existing


def _resolve_target(
    user: User,
    task: Task | None,
    file_id: uuid.UUID | None,
    assignment_id: uuid.UUID | None,
    relation: str,
    personal: bool = False,
) -> tuple[File | None, TaskAssignment | None]:
    """Check the task, the existing file (for a new version) and the assignment the upload belongs to."""
    if (
        personal
    ):  # one's own storage: every active person but HR; a new version only of one's own private file
        require(user, "file.upload_personal")
        existing = None
        if file_id:
            existing = File.objects.filter(
                pk=file_id, owner=user, visibility=FileVisibility.PRIVATE, status=FileStatus.ACTIVE
            ).first()
            if existing is None:
                raise ValidationFailed("That is not your personal file.", fields={"file_id": ["Invalid."]})
        return existing, None
    if relation == FileRelation.VOICE:  # a recording is a message: whoever may write on the task may send one
        if task is None:
            raise ValidationFailed("A voice message belongs to a task.", fields={"task_id": ["Required."]})
        require(user, "task.comment", task)
        return None, None
    require(user, "file.upload", task)
    assignment: TaskAssignment | None = None
    if user.role == Role.EMPLOYEE:
        if relation != FileRelation.SUBMISSION:
            raise PermissionDenied("You can only upload your own work to a task.", action="file.upload")
        qs = TaskAssignment.objects.filter(task=task, user=user, status__in=OPEN_STATUSES)
        assignment = qs.filter(pk=assignment_id).first() if assignment_id else qs.first()
        if assignment is None:
            raise PermissionDenied("That assignment is not yours.", action="file.upload")
    elif assignment_id:
        assignment = TaskAssignment.objects.filter(pk=assignment_id, task=task).first()
        if assignment is None:
            raise ValidationFailed(
                "That assignment is not on this task.", fields={"assignment_id": ["Invalid."]}
            )
    existing: File | None = None
    if file_id:
        existing = File.objects.filter(pk=file_id).first()
        linked = existing is not None and (
            TaskFile.objects.filter(task=task, file=existing).exists()
            if task is not None
            else existing.visibility == FileVisibility.LIBRARY
        )
        if not linked:
            raise ValidationFailed("That file is not on this task.", fields={"file_id": ["Invalid."]})
    return existing, assignment


def start_upload(
    user: User,
    *,
    key: uuid.UUID,
    task: Task | None,
    file_id: uuid.UUID | None,
    assignment_id: uuid.UUID | None,
    relation: str,
    original_filename: str,
    size: int,
    sha256: str,
    allow_blocked: bool = False,
    display_name: str = "",
    purpose: str = "",
    scope: str = "",
    note: str = "",
    workspace: Any = None,
    folder_id: uuid.UUID | None = None,
    relative_dirs: list[str] | None = None,
    conversation_id: uuid.UUID | None = None,
    request: Any = None,
) -> tuple[UploadSession, bool]:
    """Open (or find) the upload for this key: (session, created). The same key returns the same session."""
    existing = UploadSession.objects.filter(idempotency_key=key).first()
    if existing is not None:
        if existing.user_id != user.pk:
            raise PermissionDenied("Not your upload.", action="file.upload")
        if existing.expected_sha256 != (sha256 or "").lower() or existing.expected_size != size:
            raise Conflict("This upload key was already used for a different file. Use a new key.")
        return existing, False
    name = _clean_name(original_filename)
    voice = relation == FileRelation.VOICE or purpose == VOICE_PURPOSE
    if relation == FileRelation.VOICE:
        shown = _voice_name(task, name)  # the server names a recording; whatever the browser sent is ignored
    else:
        shown = _clean_name(display_name, "display_name") if display_name.strip() else ""
    if not voice:  # a system-made name (a voice note) is exempt; every person-chosen name is judged
        _require_meaningful_name(shown or name, task)
    sha = (sha256 or "").lower()
    if not SHA_RE.match(sha):
        raise ValidationFailed("The file fingerprint is not valid.", fields={"sha256": ["Invalid."]})
    max_mb = int(settings.OTS_VOICE_MAX_MB if relation == FileRelation.VOICE else settings.OTS_UPLOAD_MAX_MB)
    max_bytes = max_mb * 1024 * 1024
    if size < 1 or size > max_bytes:
        raise ValidationFailed(
            f"Files must be between 1 byte and {max_mb} MB.",
            fields={"size": ["Invalid."]},
        )
    if relation not in FileRelation.values:
        raise ValidationFailed("Choose what this file is.", fields={"relation": ["Invalid."]})
    override = False
    if is_blocked(name) or (shown and is_blocked(shown)):
        if not (allow_blocked and user.role == Role.SUPER_ADMIN):
            raise ValidationFailed(
                "This type of file is not allowed (programs and scripts).",
                fields={"original_filename": ["Blocked."]},
            )
        override = True
    personal = scope == PERSONAL
    in_workspace = scope == WORKSPACE_SCOPE
    in_library = scope == LIBRARY_SCOPE
    in_chat = scope == CHAT_SCOPE
    if in_chat:
        from ots.chat.services import membership

        membership(user, conversation_id)  # a stranger's conversation is a 404, before anything is stored
    if (personal or in_workspace or in_library or in_chat) and task is not None:
        raise ValidationFailed("That upload does not belong to a task.", fields={"scope": ["Invalid."]})
    if in_workspace and workspace is None:
        raise NotFound("Not found.")
    lib_staff = in_library and user.role != Role.SUPER_ADMIN  # staff add a new file for approval (IMP-15.5)
    if lib_staff and file_id:
        raise PermissionDenied("Only the Super Admin adds a version to a Library file.", action="file.upload")
    if in_workspace:
        target, assignment = _resolve_workspace_target(user, workspace, file_id), None
    elif lib_staff:
        target, assignment = None, None
    elif in_chat:
        require(user, "file.upload_personal")  # anyone who may have personal files may send one
        target, assignment = None, None
    else:
        target, assignment = _resolve_target(user, task, file_id, assignment_id, relation, personal)
    folder: Any = None
    if in_library and (folder_id or relative_dirs) and target is None:
        from .models import LibraryFolder

        folder = LibraryFolder.objects.filter(pk=folder_id).first() if folder_id else None
        if folder_id and folder is None:
            raise NotFound("No such folder.")
        if relative_dirs:
            from .library import ensure_path as library_path

            folder = library_path(user, relative_dirs, folder_id)
    if in_workspace and (folder_id or relative_dirs) and target is None:
        from ots.workspaces import folders as ws_folders

        folder = (
            ws_folders.ensure_path(user, workspace, relative_dirs, folder_id)
            if relative_dirs
            else ws_folders.folder_of(workspace, folder_id)
        )
    if lib_staff:
        from .library import may_upload

        if not may_upload(user, folder):
            raise PermissionDenied("You cannot upload to the Library here.", action="library.upload")
    if (
        personal and (folder_id or relative_dirs) and target is None
    ):  # into a folder; the missing ones are made
        from .folders import ensure_path

        folder = ensure_path(user, relative_dirs or [], folder_id) if relative_dirs else None
        if folder is None and folder_id:
            from .folders import my_folder

            folder = my_folder(user, folder_id)
    layout.admit(size)
    try:
        with transaction.atomic():
            session = UploadSession.objects.create(
                idempotency_key=key,
                user=user,
                task=task,
                assignment=assignment,
                file=target,
                relation=relation,
                expected_size=size,
                expected_sha256=sha,
                original_filename=name,
                scope=scope if (personal or in_workspace or in_library or in_chat) else "",
                conversation_id=conversation_id if in_chat else None,
                workspace_id=workspace.pk if in_workspace else None,
                folder_id=folder.pk if folder else None,
                note=" ".join((note or "").split())[:200],
                display_name=shown,
                override_block=override,
            )
            if override:
                audit.record(
                    "file.block_override",
                    user,
                    session,
                    {"filename": name, "task_id": str(task.pk) if task else None},
                    request,
                )
    except IntegrityError:  # two requests with the same key at once: the other one won
        return UploadSession.objects.get(idempotency_key=key, user=user), False
    path = layout.staging_path(session.pk)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o600)
    return session, True


# --- receive --------------------------------------------------------------------------------------


def _version_for(session: UploadSession) -> FileVersion:
    return FileVersion.objects.get(idempotency_key=session.idempotency_key)


def _sha_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def _drop(path: Path) -> None:
    path.unlink(missing_ok=True)


def receive_piece(
    user: User, upload_id: uuid.UUID, start: int, end: int, total: int, stream: IO[bytes], request: Any = None
) -> Progress:
    """Append bytes `start..end` (inclusive) to the staged copy, only if `start` is exactly where it ends.

    The last piece verifies the SHA-256 and commits. A wrong hash (F-FL-2) keeps nothing. If the disk lost
    bytes the database had counted, the offset is corrected and committed, and the client resumes from it.
    """
    result = _receive(user, upload_id, start, end, total, stream, request)
    if isinstance(result, int):
        raise OffsetMismatch(result)
    return result


def _receive(
    user: User, upload_id: uuid.UUID, start: int, end: int, total: int, stream: IO[bytes], request: Any
) -> Progress | int:
    rejected = False
    with transaction.atomic():
        s = UploadSession.objects.select_for_update().filter(pk=upload_id, user=user).first()
        if s is None:
            raise NotFound("No such upload.")
        if s.state == FileState.STAGED:  # the last piece arrived twice: the answer is the same version
            return Progress(s, _version_for(s))
        if s.state != FileState.RECEIVING:
            raise Conflict("This upload is closed. Start it again.", state=s.state)
        if total != s.expected_size:
            raise ValidationFailed("The total size does not match this upload.")
        length = end - start + 1
        if start < 0 or length < 1 or end >= total or length > int(settings.OTS_UPLOAD_PIECE_BYTES):
            raise ValidationFailed("That piece range is not valid.")
        if start != s.received_bytes:
            raise OffsetMismatch(s.received_bytes)
        path = layout.staging_path(s.pk)
        path.parent.mkdir(parents=True, exist_ok=True)
        on_disk = path.stat().st_size if path.exists() else 0
        if on_disk < s.received_bytes:  # the disk lost bytes the database counted: never pad with zeros
            s.received_bytes = on_disk
            s.save(update_fields=["received_bytes"])
            return on_disk  # the caller raises, after this block committed the corrected offset
        with path.open("r+b" if path.exists() else "w+b") as fh:
            fh.truncate(start)  # drop the tail of a piece that was cut off earlier
            fh.seek(start)
            left = length
            try:
                while left:
                    chunk = stream.read(min(CHUNK, left))
                    if not chunk:
                        raise ValidationFailed("The piece ended early. Send it again.")
                    fh.write(chunk)
                    left -= len(chunk)
                fh.flush()
                os.fsync(fh.fileno())
            except BaseException:
                fh.truncate(start)
                raise
        s.received_bytes = end + 1
        s.last_piece_at = now()
        s.save(update_fields=["received_bytes", "last_piece_at"])
        if s.received_bytes < total:
            return Progress(s, None)
        if _sha_of(path) != s.expected_sha256:
            s.state = FileState.REJECTED
            s.save(update_fields=["state"])
            rejected = True
        else:
            version = _commit(s, path, request)
            return Progress(s, version)
    _drop(layout.staging_path(upload_id))  # outside the transaction: the REJECTED state is kept
    if rejected:
        raise HashMismatch("The file arrived damaged. Upload it again.")
    raise AssertionError("unreachable")  # pragma: no cover


# --- commit ---------------------------------------------------------------------------------------


def _publish_bytes(part: Path, sha: str) -> Path:
    """Put the verified bytes at their content-addressed path without ever exposing a half-written file.

    A hard link makes the final path appear atomically while the part file still exists, so a failed
    transaction leaves the upload resumable. The part file is removed after the commit.
    """
    dest = layout.blob_path(sha)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        return dest
    try:
        os.link(part, dest)
    except FileExistsError:
        pass
    except OSError:  # a different disk: copy through a temporary name, then rename
        tmp = dest.with_name(dest.name + f".{uuid.uuid4().hex}.tmp")
        with part.open("rb") as src, tmp.open("wb") as out:
            shutil.copyfileobj(src, out, CHUNK)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, dest)
    return dest


def _commit(s: UploadSession, part: Path, request: Any) -> FileVersion:
    user = s.user
    sha, size = s.expected_sha256, s.expected_size
    task = s.task
    at = now()
    local = at.astimezone(IST)
    blob = (
        Blob.objects.select_for_update().filter(pk=sha).first()
    )  # first: cache eviction needs this lock too
    dest = _publish_bytes(part, sha)

    if blob is None:
        try:
            with transaction.atomic():
                blob = Blob.objects.create(sha256=sha, size_bytes=size, local_path=str(dest), cached=True)
        except IntegrityError:  # someone else stored the same content a moment ago: use their row
            blob = Blob.objects.select_for_update().get(pk=sha)
    if blob.last_accessed_at != at:  # a re-upload counts as use, for the cache's LRU order
        blob.cached, blob.local_path, blob.last_accessed_at = True, str(dest), at
        blob.save(update_fields=["cached", "local_path", "last_accessed_at"])
    if not dest.exists():  # eviction removed the copy between publishing and here: put it back
        dest = _publish_bytes(part, sha)
    archived = blob.state == FileState.ARCHIVED

    if s.file_id:
        file = File.objects.select_for_update().get(pk=s.file_id)
    else:
        file = File.objects.create(
            company=user.company,
            display_name=s.display_name or s.original_filename,
            owner=user,
            folder_id=s.folder_id if s.scope == PERSONAL else None,
            area=area_of_id(s.folder_id) if s.scope == PERSONAL else FileArea.ORGANIZED,
            library_folder_id=s.folder_id if s.scope == LIBRARY_SCOPE else None,
            library_state=(
                LibraryState.PENDING
                if s.scope == LIBRARY_SCOPE and user.role != Role.SUPER_ADMIN
                else LibraryState.APPROVED
            ),
            visibility=FileVisibility.CHAT
            if s.scope == CHAT_SCOPE
            else FileVisibility.PRIVATE
            if s.scope == PERSONAL
            else FileVisibility.WORKSPACE
            if s.scope == WORKSPACE_SCOPE
            else FileVisibility.TASK
            if task
            else FileVisibility.LIBRARY,
        )
    number = (file.versions.aggregate(m=Max("version_number"))["m"] or 0) + 1
    code = unique_code(
        make_code(
            committed_at=at,
            employee_id=user.employee_id,
            full_name=user.full_name,
            task_code=task.task_code if task else None,
            place="PERS" if s.scope == PERSONAL else _workspace_token(s),
            title=task.title if task else file.display_name,
            version=number,
            original_filename=s.original_filename,
        ),
        lambda c: FileVersion.objects.filter(file_code=c).exists(),
    )
    version = FileVersion.objects.create(
        file=file,
        version_number=number,
        file_code=code,
        original_filename=s.original_filename,
        display_name=s.display_name,
        note=s.note,
        uploaded_on=at.astimezone(IST).date(),  # the Indian calendar day: "Today" and the date folders use it
        is_voice=s.relation == FileRelation.VOICE,
        blob=blob,
        mime_type=mimetypes.guess_type(s.original_filename)[0] or "application/octet-stream",
        size_bytes=size,
        uploaded_by=user,
        uploaded_at=at,
        state=FileState.ARCHIVED if archived else FileState.STAGED,
        idempotency_key=s.idempotency_key,
    )
    file.current_version = version
    file.save(update_fields=["current_version"])
    if s.scope == WORKSPACE_SCOPE and s.workspace_id:  # the new document joins its File ID (no copy, a link)
        from ots.workspaces.models import FileIdWorkspace
        from ots.workspaces.services import link_new_file

        link_new_file(
            user, FileIdWorkspace.objects.get(pk=s.workspace_id), file, request,
            folder_id=s.folder_id,
        )  # fmt: skip

    body = {
        "file_id": str(file.pk),
        "version_id": str(version.pk),
        "version": number,
        "file_code": code,
        "filename": s.original_filename,
        "relation": s.relation,
        "size": size,
    }
    if task is not None and s.relation != FileRelation.VOICE:  # a voice note is a message, not a task file
        TaskFile.objects.create(
            task=task,
            file=file,
            version=version,
            assignment=s.assignment,
            relation=s.relation,
            linked_by=user,
        )
        TaskEvent.objects.create(
            task=task, assignment=s.assignment, actor=user, event_type="file.version_added", payload=body,
            created_at=at,
        )  # fmt: skip
        events.publish("file.version_added", ("task", str(task.pk)), {**body, "task_id": str(task.pk)})

    pending = (
        Op.objects.filter(kind=Op.Kind.ARCHIVE_FILE, sha256=sha)
        .exclude(state__in=[Op.State.DONE, Op.State.FAILED])
        .exists()
    )
    if not archived and not pending:  # one upload per content; M08.4 marks every version of the blob archived
        queue.enqueue(
            Op.Kind.ARCHIVE_FILE,
            Library.BUSINESS,
            dest,
            f"{local:%Y}/{local:%m}/{code}",
            sha,
            size,
            ref_type="file_version",
            ref_id=str(version.pk),
        )
    if (
        archived
    ):  # content SharePoint already holds: this version is off-site now, and the manifest must say so
        events.publish("file.archived", ("file_version", str(version.pk)), body)
        transaction.on_commit(lambda: _list_in_manifest(version, blob, at))
    audit.record(
        "file.uploaded", user, version, {k: body[k] for k in ("file_code", "version", "size")}, request
    )
    events.publish("file.staged", ("file_version", str(version.pk)), body)
    if file.library_state == LibraryState.PENDING and not s.file_id:
        from .library import notify_pending

        notify_pending(file, file.library_folder, request)
    s.state = FileState.STAGED
    s.save(update_fields=["state"])
    transaction.on_commit(lambda: _drop(part))
    transaction.on_commit(_trim_cache)
    return version


def _list_in_manifest(version: FileVersion, blob: Blob, at: datetime.datetime) -> None:
    """The disaster-recovery list of what is in SharePoint. After the commit, so it never names a version the
    database rolled back; a failure is logged and the nightly inventory still finds the file."""
    try:
        manifest.append(version.file_code, blob.sha256, blob.size_bytes, blob.drive_id, blob.item_id, at)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception("could not append to the manifest")


def _trim_cache() -> None:
    """After an upload: if the cache is above 90% of its cap, evict down to 80%. Never fails the upload."""
    try:
        cache.run()
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception("cache trim failed")


# --- status, cancel, sweep ------------------------------------------------------------------------


def get_session(user: User, upload_id: uuid.UUID) -> Progress:
    s = UploadSession.objects.filter(pk=upload_id, user=user).first()
    if s is None:
        raise NotFound("No such upload.")
    return Progress(s, _version_for(s) if s.state == FileState.STAGED else None)


def cancel_upload(user: User, upload_id: uuid.UUID) -> UploadSession:
    with transaction.atomic():
        s = UploadSession.objects.select_for_update().filter(pk=upload_id, user=user).first()
        if s is None:
            raise NotFound("No such upload.")
        if s.state != FileState.RECEIVING:
            raise Conflict("This upload is already closed.", state=s.state)
        s.state = FileState.ABANDONED
        s.save(update_fields=["state"])
    _drop(layout.staging_path(upload_id))
    return s


def staging_sweep(at: datetime.datetime | None = None) -> dict[str, int]:
    """Close uploads idle for 24 hours and delete their part files, plus part files no open upload owns."""
    at = at or now()
    stale = UploadSession.objects.filter(state=FileState.RECEIVING, last_piece_at__lt=at - ABANDON_AFTER)
    ids = list(stale.values_list("pk", flat=True))
    closed = stale.update(state=FileState.ABANDONED)
    for pk in ids:
        _drop(layout.staging_path(pk))
    open_ids = {
        str(p) for p in UploadSession.objects.filter(state=FileState.RECEIVING).values_list("pk", flat=True)
    }
    strays = 0
    root = Path(settings.OTS_STAGING_DIR)
    cutoff = at.timestamp() - ABANDON_AFTER.total_seconds()
    for part in root.glob("*.part") if root.exists() else []:
        if part.stem not in open_ids and part.stat().st_mtime < cutoff:
            _drop(part)
            strays += 1
    return {"abandoned": closed, "stray_parts": strays}
