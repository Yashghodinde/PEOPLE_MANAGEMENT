"""File reads (M08). Every query starts from what the viewer may see."""

from __future__ import annotations

import uuid
from typing import Any

from django.db.models import Exists, F, OuterRef, Q, QuerySet

from ots.files.models import FileVersion, TaskFile
from ots.tasks.models import Task

PANEL_LIMIT = 200  # files shown on one task page (CLAUDE.md: never an unbounded list)

PENDING_STATES = ("STAGED", "ARCHIVING", "RETRY_WAIT", "VERIFYING", "BLOCKED_QUOTA", "BLOCKED_AUTH")


def task_file_rows(viewer: Any, task: Task) -> list[TaskFile]:
    """Newest first, one row per upload linked to this task. The task must already be visible to `viewer`."""
    if not Task.objects.visible_to(viewer).filter(pk=task.pk).exists():
        return []
    return list(
        TaskFile.objects.filter(task=task)
        .select_related("file", "version", "version__uploaded_by", "assignment__user")
        .order_by("-linked_at", "-id")[:PANEL_LIMIT]
    )


def anything_pending(rows: list[TaskFile]) -> bool:
    return any(r.version.state in PENDING_STATES for r in rows)


def versions_visible_to(viewer: Any) -> QuerySet[FileVersion]:
    """Versions `viewer` may download or preview (M08 section 8).

    Super Admin: all. Everyone else: their own uploads, files on tasks they can see, files handed to them, and
    the library. A Sub-Admin also sees uploads by anyone in their team.
    """
    from ots.accounts import selectors as accounts
    from ots.accounts.models import Role
    from ots.files.models import FileHandoverRecipient, FileVisibility

    if viewer is None or not getattr(viewer, "is_authenticated", False) or not viewer.is_active:
        return FileVersion.objects.none()
    qs = FileVersion.objects.all()
    if viewer.role == Role.SUPER_ADMIN:
        return qs
    visible_tasks = Task.objects.visible_to(viewer).values("pk")
    wanted = (
        Q(uploaded_by_id=viewer.pk)
        | Q(file__visibility=FileVisibility.LIBRARY)
        | Exists(TaskFile.objects.filter(version=OuterRef("pk"), task__in=visible_tasks))
        | Exists(FileHandoverRecipient.objects.filter(handover__version=OuterRef("pk"), to_user_id=viewer.pk))
    )
    if viewer.role == Role.SUB_ADMIN:
        wanted |= Q(uploaded_by_id__in=accounts.subtree_ids(viewer.pk))
    return qs.filter(wanted)


def handover_targets(sender: Any) -> QuerySet[Any]:
    """Whom `sender` may send a file to (M08 section 8): Super/Sub Admin anyone active; an Employee their
    manager and the people assigned to the same tasks."""
    from ots.accounts.models import Role, User
    from ots.tasks.models import TaskAssignment

    if sender is None or not getattr(sender, "is_active", False):
        return User.objects.none()
    others = User.objects.filter(is_active=True).exclude(pk=sender.pk)
    if sender.role in (Role.SUPER_ADMIN, Role.SUB_ADMIN):
        return others
    mine = TaskAssignment.objects.filter(user=sender).values("task_id")
    mates = TaskAssignment.objects.filter(task_id__in=mine).values("user_id")
    return others.filter(Q(pk=sender.reports_to_id) | Q(pk__in=mates))


TRAIL_LIMIT = 200  # per source: the page is never unbounded


def trail(viewer: Any, file_id: Any) -> list[dict[str, Any]] | None:
    """Everything that happened to one file, newest first, over the versions `viewer` may see.

    Uploads, archiving, handovers sent and first opened, downloads and previews, task links, and the
    storage problems (missing, drifted, repaired). None when the viewer may see no version of it.
    Employees see only their own downloads; admins see everyone's.
    """
    from ots.accounts.models import Role
    from ots.audit.models import AuditLog
    from ots.files.models import FileAccessLog, FileHandover

    versions = list(
        versions_visible_to(viewer)
        .filter(file_id=file_id)
        .select_related("uploaded_by", "blob")
        .order_by("version_number")
    )
    if not versions:
        return None
    by_id = {v.pk: v for v in versions}
    ids = list(by_id)
    out: list[dict[str, Any]] = []

    def add(at: Any, kind: str, text: str, v: Any) -> None:
        out.append({"at": at, "kind": kind, "text": text, "version": v.version_number})

    for v in versions:
        add(v.uploaded_at, "uploaded", f"{v.uploaded_by.full_name} uploaded version {v.version_number}", v)
        if v.blob.archived_at is not None:
            add(v.blob.archived_at, "archived", f"Version {v.version_number} is stored off-site", v)
    handovers = (
        FileHandover.objects.filter(version_id__in=ids)
        .select_related("from_user")
        .prefetch_related("recipients__to_user")
        .order_by("-sent_at")[:TRAIL_LIMIT]
    )
    for h in handovers:
        v = by_id[h.version_id]
        names = ", ".join(r.to_user.full_name for r in h.recipients.all())
        note = f" (“{h.note}”)" if h.note else ""
        add(
            h.sent_at,
            "handover",
            f"{h.from_user.full_name} sent version {v.version_number} to {names}{note}",
            v,
        )
        for r in h.recipients.all():
            if r.first_opened_at is not None:
                add(r.first_opened_at, "opened", f"{r.to_user.full_name} opened it for the first time", v)
    links = TaskFile.objects.filter(version_id__in=ids).select_related("task", "linked_by")
    for t in links.order_by("-linked_at")[:TRAIL_LIMIT]:
        add(
            t.linked_at,
            "linked",
            f"Linked to task {t.task.title} as {t.relation.lower()}",
            by_id[t.version_id],
        )
    log = FileAccessLog.objects.filter(version_id__in=ids).select_related("user").order_by("-id")
    if viewer.role == Role.EMPLOYEE:
        log = log.filter(user_id=viewer.pk)
    for a in log[:TRAIL_LIMIT]:
        verb = "downloaded" if a.action == "DOWNLOAD" else "previewed"
        add(a.at, "access", f"{a.user.full_name} {verb} it", by_id[a.version_id])
    problems = AuditLog.objects.filter(
        target_type="fileversion",
        target_id__in=[str(i) for i in ids],
        action__in=["file.missing", "file.drifted", "file.repaired"],
    ).order_by("-id")[:TRAIL_LIMIT]
    words = {
        "file.missing": "Missing from SharePoint",
        "file.drifted": "Changed in SharePoint",
        "file.repaired": "Restored to SharePoint",
    }
    for p in problems:
        add(p.at, "problem", words[p.action], by_id[uuid.UUID(p.target_id)])
    out.sort(key=lambda e: e["at"], reverse=True)
    return out


# --- library and search ---

SEARCH_PAGE = 50
SEARCH_MAX = 200
MAX_TERMS = 6


def _like(term: str) -> str:
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def search(
    viewer: Any,
    q: str = "",
    *,
    uploader: Any = None,
    task: Any = None,
    extension: str = "",
    relation: str = "",
    state: str = "",
    date_from: Any = None,
    date_to: Any = None,
    limit: int = SEARCH_PAGE,
    offset: int = 0,
) -> list[FileVersion]:
    """Versions matching every word of `q` (in the file name, file code, task title or uploader name),
    narrowed by the filters, newest first, only those `viewer` may open. At most `limit` (never over 200)."""
    qs = versions_visible_to(viewer)
    for term in q.split()[:MAX_TERMS]:
        like = _like(term)
        # one indexed branch per place a word can be, joined by UNION: an OR across tables cannot use an index
        from ots.accounts.models import User

        by_name = FileVersion.objects.filter(original_filename__ilike=like).values("pk")
        by_code = FileVersion.objects.filter(file_code__ilike=like).values("pk")
        by_person = FileVersion.objects.filter(
            uploaded_by_id__in=User.objects.filter(full_name__ilike=like).values("pk")
        ).values("pk")
        by_task = TaskFile.objects.filter(task__title__ilike=like).values(pk=F("version_id"))
        qs = qs.filter(pk__in=by_name.union(by_code, by_person, by_task))
    if uploader:
        qs = qs.filter(uploaded_by_id=uploader)
    if task:
        qs = qs.filter(Exists(TaskFile.objects.filter(version=OuterRef("pk"), task_id=task)))
    if relation:
        qs = qs.filter(Exists(TaskFile.objects.filter(version=OuterRef("pk"), relation=relation)))
    if extension:
        qs = qs.filter(file_code__iendswith="." + extension.lstrip(".").lower())
    if state:
        qs = qs.filter(state=state)
    if date_from:
        qs = qs.filter(uploaded_at__date__gte=date_from)
    if date_to:
        qs = qs.filter(uploaded_at__date__lte=date_to)
    limit = max(1, min(int(limit), SEARCH_MAX))
    return list(
        qs.select_related("file", "uploaded_by").order_by("-uploaded_at", "-id")[
            max(0, offset) : offset + limit
        ]
    )


def library_view(folder_id: Any = None) -> dict[str, Any]:
    """One level of the library: subfolders, files in it, and the path above it. Everyone may read it."""
    from ots.files.models import File, LibraryFolder

    folder = LibraryFolder.objects.filter(pk=folder_id).first() if folder_id else None
    if folder_id and folder is None:
        return {}
    path: list[Any] = []
    cur = folder
    while cur is not None and len(path) < 20:
        path.append(cur)
        cur = cur.parent
    files = (
        File.objects.filter(visibility="LIBRARY", library_folder=folder)
        .select_related("current_version", "current_version__uploaded_by")
        .order_by("display_name")[:PANEL_LIMIT]
    )
    return {
        "folder": folder,
        "path": list(reversed(path)),
        "folders": list(LibraryFolder.objects.filter(parent=folder).order_by("name")[:PANEL_LIMIT]),
        "files": list(files),
    }
