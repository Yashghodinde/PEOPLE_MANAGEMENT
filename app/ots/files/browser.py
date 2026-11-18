"""What the shared file manager lists (IMP-14.3). One builder per section turns its rows into the same item
dicts, so `templates/files/_manager.html` is written once. Reads only."""

from __future__ import annotations

import datetime
import json
from typing import Any
from urllib.parse import quote, urlencode

from django.db.models import Count

from ots.core.clock import IST, today_local

from . import folders as folder_svc
from . import personal
from .models import File, FileStatus, FileVersion, FileVisibility, Folder, FolderContext

ACCESS_PERSONAL = "Only you and the Super Admin"


def long_day(d: datetime.date) -> str:
    return f"{d.day} {d:%B %Y}"


def _ext(name: str) -> str:
    return name.rsplit(".", 1)[-1].upper() if "." in name else "FILE"


def folder_item(
    f: Folder, counts: tuple[int, int], url: str, location: str, access: str, rename_url: str = ""
) -> dict[str, Any]:
    n_folders, n_files = counts
    count = n_folders + n_files
    d = {
        "kind": "folder", "id": str(f.pk), "name": f.name, "url": url, "type": "Folder",
        "modified": f.updated_at, "by": f.created_by.full_name, "size": None,
        "status": "", "versions": None, "items": count, "location": location, "access": access,
        "created": f.created_at, "folder_id": str(f.pk),
        "can_rename": bool(rename_url), "can_move": bool(rename_url),
        "rename_url": rename_url, "hide_url": "",
    }  # fmt: skip
    d["details"] = json.dumps(
        {
            "Name": f.name,
            "Folder ID": str(f.pk)[:8],
            "Parent": location,
            "Created by": d["by"],
            "Created": f"{f.created_at.astimezone(IST):%d %b %Y, %H:%M}",
            "Modified": f"{f.updated_at.astimezone(IST):%d %b %Y, %H:%M}",
            "Items": str(count),
            "Access": access,
        }
    )
    return d


def version_item(
    v: FileVersion, location: str, access: str, *, mine: bool, sent_by: str = ""
) -> dict[str, Any]:
    f = v.file
    name = f.display_name
    d = {
        "kind": "file", "id": str(f.pk), "version_id": str(v.pk), "name": name,
        "url": f"/files/v/{v.pk}/preview", "download": f"/files/v/{v.pk}/download",
        "history": f"/files/{f.pk}/trail", "send": f"/files/v/{v.pk}/handover",
        "type": _ext(v.original_filename or name), "modified": v.uploaded_at, "by": v.uploaded_by.full_name,
        "size": v.size_bytes, "status": v.state, "versions": v.version_number, "items": None,
        "location": location, "access": access, "sent_by": sent_by,
        "can_rename": mine, "can_move": mine, "can_hide": mine, "can_send": mine,
        "rename_url": f"/files/{f.pk}/rename", "hide_url": f"/files/{f.pk}/hide",
        "share_url": f"/files/v/{v.pk}/sharing" if mine else "",
    }  # fmt: skip
    d["details"] = json.dumps(
        {
            "Name": name,
            "File ID": v.file_code,
            "Type": d["type"],
            "Size": f"{v.size_bytes:,} bytes",
            "Uploaded by": d["by"],
            "Uploaded": f"{v.uploaded_at.astimezone(IST):%d %b %Y, %H:%M}",
            "Location": location,
            "Version": str(v.version_number),
            "Storage": v.state.title(),
            "Access": access,
        }
    )
    return d


def _folder_counts(ids: list[Any]) -> dict[Any, tuple[int, int]]:
    kids = dict(
        Folder.objects.filter(parent_id__in=ids, status=FileStatus.ACTIVE)
        .values_list("parent_id")
        .annotate(n=Count("pk"))
    )
    files = dict(
        File.objects.filter(folder_id__in=ids, status=FileStatus.ACTIVE)
        .values_list("folder_id")
        .annotate(n=Count("pk"))
    )
    return {i: (kids.get(i, 0), files.get(i, 0)) for i in ids}


def _children(user: Any, parent: Folder | None, day: datetime.date | None) -> list[Folder]:
    qs = Folder.objects.filter(
        context=FolderContext.PERSONAL, owner=user, parent=parent, status=FileStatus.ACTIVE
    ).select_related("created_by")
    if parent is None:
        qs = qs.filter(on_date=day)
    return list(qs.order_by("name_key")[:500])


PERSONAL_BASE: dict[str, Any] = {
    "scope": "personal",
    "workspace_id": "",
    "create_url": "/files/mine/folders",
    "create_hidden": [],
    "move_url": "/files/mine/move",
    "actions": [],
    "next": "",
}


def personal_day(user: Any, day: datetime.date) -> dict[str, Any]:
    """A date: its root folders, then files uploaded that day that are not in a folder."""
    label = long_day(day)
    fs = _children(user, None, day)
    counts = _folder_counts([f.pk for f in fs])
    items = [
        folder_item(
            f,
            counts[f.pk],
            f"/files/mine/f/{f.pk}",
            label,
            ACCESS_PERSONAL,
            f"/files/mine/folders/{f.pk}/rename",
        )
        for f in fs
    ]
    versions = personal.versions_on(user, day)
    items += [
        version_item(v, label, ACCESS_PERSONAL, mine=True) for v in versions if v.file.folder_id is None
    ]
    today = day == today_local()
    return {
        "title": label,
        "items": items,
        "can_upload": today,
        "can_create": today,
        "parent_id": "",
        "crumbs": [("Files", "/files/mine"), ("My storage", "/files/mine"), (label, "")],
        "empty": "This folder is empty." if today else "Nothing from this day.",
        "note": "" if today else "New files go to Today. Open a folder to add files to it.",
        "section": "mine",
        "move_day": day,
        "show_disabled_create": not today,
        **PERSONAL_BASE,
        "create_hidden": [("day", f"{day:%Y-%m-%d}")],
    }


def personal_folder(user: Any, folder: Folder) -> dict[str, Any]:
    chain = folder_svc.ancestors(folder)
    day = chain[0].on_date or today_local()
    crumbs = [
        ("Files", "/files/mine"),
        ("My storage", "/files/mine"),
        (long_day(day), f"/files/mine/{day:%Y-%m-%d}"),
    ]
    crumbs += [(c.name, f"/files/mine/f/{c.pk}") for c in chain[:-1]] + [(chain[-1].name, "")]
    location = " > ".join(["My storage", long_day(day), *[c.name for c in chain]])
    fs = _children(user, folder, None)
    counts = _folder_counts([f.pk for f in fs])
    items = [
        folder_item(
            f,
            counts[f.pk],
            f"/files/mine/f/{f.pk}",
            location,
            ACCESS_PERSONAL,
            f"/files/mine/folders/{f.pk}/rename",
        )
        for f in fs
    ]
    versions = [
        f.current_version
        for f in File.objects.filter(folder=folder, owner=user, status=FileStatus.ACTIVE)
        .select_related("current_version__file", "current_version__uploaded_by", "current_version__blob")
        .order_by("-created_at")[:500]
        if f.current_version is not None and not f.current_version.is_voice
    ]
    items += [version_item(v, location, ACCESS_PERSONAL, mine=True) for v in versions]
    return {
        "title": folder.name, "items": items, "can_upload": True, "can_create": True,
        "parent_id": str(folder.pk), "crumbs": crumbs, "empty": "This folder is empty.", "note": "",
        "section": "mine", "move_day": day, **PERSONAL_BASE,
    }  # fmt: skip


def personal_root(user: Any, page: int = 0) -> dict[str, Any]:
    """Today first, then earlier dates newest first (each a folder)."""
    today = today_local()
    days, more = personal.date_folders(user, page)
    items: list[dict[str, Any]] = []
    now_dt = datetime.datetime.now(IST)
    if page == 0:
        items.append(_day_item(today, f"Today — {today:%A}, {long_day(today)}", None, now_dt))
    items += [_day_item(d, long_day(d), n, now_dt) for d, n in days]
    return {
        "title": "My storage", "items": items, "can_upload": False, "can_create": False, "parent_id": "",
        "crumbs": [], "empty": "", "note": "", "section": "mine", "more": more, "page": page,
        "dates": True, "show_disabled_create": True, **PERSONAL_BASE,
    }  # fmt: skip


def _day_item(d: datetime.date, name: str, n: int | None, now_dt: datetime.datetime) -> dict[str, Any]:
    stamp = datetime.datetime.combine(d, datetime.time(23, 59), tzinfo=IST)
    return {
        "kind": "folder", "id": f"day-{d}", "name": name, "type": "Folder",
        "url": f"/files/mine/{d:%Y-%m-%d}",
        "modified": stamp, "by": "", "size": None, "status": "", "versions": None, "items": n,
        "location": "My storage", "access": ACCESS_PERSONAL, "can_rename": False, "can_move": False,
        "rename_url": "", "hide_url": "",
        "details": json.dumps({"Name": name, "Location": "My storage", "Access": ACCESS_PERSONAL}),
    }  # fmt: skip


def move_targets(user: Any, limit: int = 300) -> list[tuple[str, str]]:
    """(id, 'Date > Folder > Sub') for the person's own folders, for the Move dialog."""
    rows = list(
        Folder.objects.filter(context=FolderContext.PERSONAL, owner=user, status=FileStatus.ACTIVE)
        .order_by("-created_at")
        .values("pk", "parent_id", "name", "on_date")[:limit]
    )
    by_id = {r["pk"]: r for r in rows}
    out = []
    for r in rows:
        parts, cur, guard = [], r, 0
        while cur is not None and guard < 10:
            parts.append(cur["name"])
            if cur["parent_id"] is None:
                parts.append(long_day(cur["on_date"]) if cur["on_date"] else "")
                break
            cur = by_id.get(cur["parent_id"])
            guard += 1
        out.append((str(r["pk"]), " > ".join(p for p in reversed(parts) if p)))
    return sorted(out, key=lambda t: t[1])


# ---------------------------------------------------------------- shared with me, task files (IMP-14.5)

NO_WRITE: dict[str, Any] = {
    "scope": "",
    "workspace_id": "",
    "create_url": "",
    "create_hidden": [],
    "move_url": "",
    "actions": [],
    "next": "",
    "can_upload": False,
    "can_create": False,
    "parent_id": "",
    "show_disabled_create": False,
    "note": "",
    "crumbs": [],
}


def shared_with_me(user: Any) -> dict[str, Any]:
    items = []
    for v in personal.shared_with_me(user):
        who = v.sent_by.full_name  # type: ignore[attr-defined]
        d = version_item(
            v, f"Sent to you by {who}", "Shared with you; you may open and download it", mine=False
        )
        d["by"], d["modified"] = who, v.sent_at  # type: ignore[attr-defined]
        d["can_send"] = True  # passing it on is a new hand-over, which the server checks again
        d["details"] = json.dumps(
            {
                **json.loads(d["details"]),
                "Shared by": who,
                "Shared on": f"{v.sent_at.astimezone(IST):%d %b %Y, %H:%M}",  # type: ignore[attr-defined]
                "Permission": "View and download",
            }
        )
        items.append(d)
    return {
        "title": "Shared with me", "items": items, "empty": "Nothing has been shared with you.",
        "section": "shared", "column_by": "Shared by", **NO_WRITE,
    }  # fmt: skip


def _task_name(t: Any) -> str:
    return f"{t.task_code}: {t.title}"


def _tq(day: datetime.date, person: str = "") -> str:
    return "?" + urlencode({"day": f"{day:%Y-%m-%d}", **({"p": person} if person else {})})


def _doc_count(tasks: list[Any]) -> int:
    return sum(len(t.documents) for t in tasks)


def _newest(tasks: list[Any]) -> Any:
    return max((r.version.uploaded_at for t in tasks for d in t.documents for r in d.rows), default=None)


def _task_folder(day: datetime.date, tb: Any, person: str = "") -> dict[str, Any]:
    name = _task_name(tb.task)
    return {
        "kind": "folder", "id": f"task-{tb.task.pk}", "name": name, "type": "Task",
        "url": f"/files/tasks/{tb.task.pk}{_tq(day, person)}", "modified": _newest([tb]), "by": "",
        "size": None, "status": "", "versions": None, "items": len(tb.documents), "can_rename": False,
        "can_move": False, "rename_url": "", "hide_url": "",
        "details": json.dumps({"Task": name, "Documents": str(len(tb.documents)), "Day": long_day(day)}),
    }  # fmt: skip


def task_root(viewer: Any, before: datetime.date | None) -> dict[str, Any]:
    from . import taskstorage

    days, next_before = taskstorage.task_storage(viewer, before)
    items = []
    for blk in days:
        tasks = blk.tasks + [t for p in blk.people for t in p.tasks]
        name = long_day(blk.day)
        items.append({
            "kind": "folder", "id": f"day-{blk.day}", "name": name, "type": "Folder",
            "url": f"/files/tasks/d/{blk.day:%Y-%m-%d}", "modified": _newest(tasks), "by": "", "size": None,
            "status": "", "versions": None, "items": _doc_count(tasks), "can_rename": False,
            "can_move": False,
            "rename_url": "", "hide_url": "",
            "details": json.dumps({"Name": name, "Documents": str(_doc_count(tasks))}),
        })  # fmt: skip
    return {
        "title": "Task files", "items": items, "section": "tasks", "dates": True,
        "empty": "No task files yet.", "more": next_before is not None, "next_before": next_before,
        **NO_WRITE,
    }  # fmt: skip


def _day_block(viewer: Any, day: datetime.date) -> Any:
    from . import taskstorage

    days, _ = taskstorage.task_storage(viewer, day + datetime.timedelta(days=1))
    return days[0] if days and days[0].day == day else None


def task_day(viewer: Any, day: datetime.date, person: str = "") -> dict[str, Any]:
    """A date: a manager sees employees, an employee sees tasks. With `person`: that employee's tasks."""
    blk = _day_block(viewer, day)
    label = long_day(day)
    crumbs = [
        ("Files", "/files/mine"),
        ("Task files", "/files/tasks"),
        (label, f"/files/tasks/d/{day:%Y-%m-%d}" if person else ""),
    ]
    items: list[dict[str, Any]] = []
    if blk is not None:
        if person:
            crumbs.append((person, ""))
            pb = next((p for p in blk.people if p.name == person), None)
            items = [_task_folder(day, tb, person) for tb in (pb.tasks if pb else [])]
        elif blk.people:
            for pb in blk.people:
                items.append({
                    "kind": "folder", "id": f"p-{pb.name}", "name": pb.name, "type": "Employee",
                    "url": f"/files/tasks/d/{day:%Y-%m-%d}?p={quote(pb.name)}",
                    "modified": _newest(pb.tasks), "by": "", "size": None, "status": "", "versions": None,
                    "items": _doc_count(pb.tasks), "can_rename": False, "can_move": False, "rename_url": "",
                    "hide_url": "", "details": json.dumps({"Employee": pb.name, "Tasks": str(len(pb.tasks))}),
                })  # fmt: skip
        else:
            items = [_task_folder(day, tb) for tb in blk.tasks]
    return {
        "title": person or label, "items": items, "section": "tasks", "crumbs": crumbs,
        "empty": "No files on this day.", **{k: v for k, v in NO_WRITE.items() if k != "crumbs"},
    }  # fmt: skip


def task_exchange(viewer: Any, task: Any, day: datetime.date | None, person: str) -> dict[str, Any]:
    from . import taskstorage

    crumbs = [("Files", "/files/mine"), ("Task files", "/files/tasks")]
    if day:
        crumbs.append((long_day(day), f"/files/tasks/d/{day:%Y-%m-%d}"))
        if person:
            crumbs.append((person, f"/files/tasks/d/{day:%Y-%m-%d}?p={quote(person)}"))
    name = _task_name(task)
    crumbs.append((name, ""))
    items = []
    for doc in taskstorage.document_groups(viewer, task):
        last = doc.rows[-1]
        d = version_item(last.version, name, "People on this task, their managers and the boss", mine=False)
        d["by"] = f"{last.version.uploaded_by.full_name} ({last.uploader_label})"
        d["details"] = json.dumps(
            {**json.loads(d["details"]), "Action": last.action, "Note": last.version.note or "None"}
        )
        items.append(d)
    return {
        "title": name, "items": items, "section": "tasks", "crumbs": crumbs,
        "empty": "No files you may see on this task.", "actions": [("Open task", f"/app/tasks/{task.pk}")],
        **{k: v for k, v in NO_WRITE.items() if k not in ("crumbs", "actions")},
    }  # fmt: skip


# ---------------------------------------------------------------- the library (IMP-14.8, layout)

ACCESS_LIBRARY = "Everyone in the company may open and download it"


def _lib_path(folder: Any) -> list[Any]:
    out, cur = [], folder
    while cur is not None and len(out) < 20:
        out.append(cur)
        cur = cur.parent
    return out[::-1]


def library(user: Any, folder_id: Any = None) -> dict[str, Any]:
    """One level of the Library. Published files are seen by everyone; a file waiting for approval is pale and
    seen only by its sender, the managers above them and the Super Admin, who also get Approve and Reject."""
    from ots.accounts import selectors as accounts
    from ots.accounts.models import Role
    from ots.core.permissions import can

    from . import library as lib
    from . import selectors
    from .models import LibraryFolder, LibraryState

    folder = LibraryFolder.objects.filter(pk=folder_id).first() if folder_id else None
    if folder_id and folder is None:
        return {}
    manage = can(user, "library.manage", None)
    may = lib.may_upload(user, folder)
    chain = _lib_path(folder)
    where = " > ".join(["Library", *[c.name for c in chain]])
    crumbs = [("Files", "/files/mine"), ("Library", "/files/library" if chain else "")]
    crumbs += [(c.name, f"/files/library?folder={c.pk}") for c in chain[:-1]]
    if chain:
        crumbs.append((chain[-1].name, ""))
    subs = list(LibraryFolder.objects.filter(parent=folder).order_by("name")[:500])
    nfold = dict(
        LibraryFolder.objects.filter(parent_id__in=[f.pk for f in subs])
        .values_list("parent_id")
        .annotate(n=Count("pk"))
    )
    nfile = dict(
        File.objects.filter(
            visibility=FileVisibility.LIBRARY, library_state=LibraryState.APPROVED,
            library_folder_id__in=[f.pk for f in subs],
        )
        .values_list("library_folder_id")
        .annotate(n=Count("pk"))
    )  # fmt: skip
    items = []
    for f in subs:
        n = nfold.get(f.pk, 0) + nfile.get(f.pk, 0)
        allows = (
            "Staff may add files (they wait for approval)"
            if f.employee_upload
            else "Only the Super Admin adds files"
        )
        items.append(
            {
                "kind": "folder", "id": str(f.pk), "name": f.name, "type": "Folder",
                "url": f"/files/library?folder={f.pk}", "modified": None, "by": "", "size": None,
                "status": "", "versions": None, "items": n, "can_rename": manage, "can_move": False,
                "rename_url": f"/files/library/folders/{f.pk}/rename", "hide_url": "",
                "toggle_url": f"/files/library/folders/{f.pk}/uploads" if manage else "",
                "toggle_value": "0" if f.employee_upload else "1",
                "toggle_label": "Stop staff uploads" if f.employee_upload else "Allow staff uploads",
                "details": json.dumps(
                    {
                        "Name": f.name, "Parent": where, "Items": str(n), "Access": ACCESS_LIBRARY,
                        "Uploads": allows,
                    }
                ),
            }
        )  # fmt: skip
    visible = selectors.versions_visible_to(user).values("pk")
    files = (
        File.objects.filter(
            visibility=FileVisibility.LIBRARY, library_folder=folder, status=FileStatus.ACTIVE,
            current_version__in=visible,
        )
        .exclude(library_state=LibraryState.REJECTED)
        .select_related("current_version__file", "current_version__uploaded_by", "owner")
        .order_by("display_name")[:500]
    )  # fmt: skip
    team = accounts.subtree_ids(user.pk) if user.role == Role.SUB_ADMIN else set()
    for f in files:
        v = f.current_version
        if v is None:
            continue
        pending = f.library_state == LibraryState.PENDING
        d = version_item(v, where, "Waiting for approval" if pending else ACCESS_LIBRARY, mine=False)
        d["can_move"] = manage and not pending
        d["pending"] = pending
        if pending:
            reviewer = user.role == Role.SUPER_ADMIN or f.owner_id in team
            d["can_review"] = reviewer
            d["approve_url"] = f"/files/library/files/{f.pk}/approve"
            d["reject_url"] = f"/files/library/files/{f.pk}/reject"
            d["details"] = json.dumps(
                {**json.loads(d["details"]), "Status": "Waiting for approval", "Sent by": f.owner.full_name}
            )
        items.append(d)
    return {
        "title": folder.name if folder else "Library", "items": items, "section": "library",
        "crumbs": crumbs if chain else [], "scope": "library", "workspace_id": "",
        "can_upload": may, "can_create": may, "create_url": "/files/library/folders" if manage else "",
        "create_hidden": [], "move_url": "/files/library/move", "actions": [], "next": "",
        "parent_id": str(folder.pk) if folder else "", "show_disabled_create": not may,
        "no_folder_upload": not manage, "empty": "This folder is empty.",
        "note": "" if manage or not may else "Files you add wait for your manager's approval first.",
    }  # fmt: skip


def library_move_targets() -> list[tuple[str, str]]:
    from .models import LibraryFolder

    rows = {f["pk"]: f for f in LibraryFolder.objects.values("pk", "parent_id", "name")[:500]}
    out = []
    for r in rows.values():
        parts, cur, guard = [], r, 0
        while cur is not None and guard < 20:
            parts.append(cur["name"])
            cur = rows.get(cur["parent_id"])
            guard += 1
        out.append((str(r["pk"]), "Library > " + " > ".join(reversed(parts))))
    return sorted(out, key=lambda t: t[1])


# ---------------------------------------------------------------- File IDs (IMP-14.6, layout)


def workspace_list(user: Any, can_create: bool) -> dict[str, Any]:
    from ots.workspaces import selectors as ws_sel

    items = []
    for w in ws_sel.cards(user):
        arch = " (archived)" if w.status == "ARCHIVED" else ""
        items.append(
            {
                "kind": "folder", "id": str(w.pk), "name": w.name, "sub": f"{w.file_id}{arch}",
                "type": w.get_category_display(), "url": f"/files/ids/{w.pk}", "modified": w.created_at,
                "by": w.owner.full_name, "size": None, "status": "", "versions": None,
                "items": w.document_count, "can_rename": False, "can_move": False, "rename_url": "",
                "hide_url": "",
                "details": json.dumps(
                    {
                        "Name": w.name, "File ID": w.file_id, "Category": w.get_category_display(),
                        "Owner": w.owner.full_name, "Members": str(w.member_count),
                        "Documents": str(w.document_count), "Access": "Members of this File ID only",
                    }
                ),
            }
        )  # fmt: skip
    return {
        "title": "File IDs", "items": items, "section": "ids", "column_by": "Owner",
        "empty": "You are not in any File ID yet.",
        **NO_WRITE, "actions": [("New File ID", "/files/ids/new")] if can_create else [],
    }  # fmt: skip


def _ws_path(ws: Any, folder: Folder | None) -> list[Folder]:
    return folder_svc.ancestors(folder) if folder is not None else []


def workspace_files(ws: Any, role: str, can_write: bool, folder: Folder | None) -> dict[str, Any]:
    from ots.workspaces import selectors as ws_sel
    from ots.workspaces.models import FileIdDocument

    chain = _ws_path(ws, folder)
    where = " > ".join([ws.name, *[c.name for c in chain]])
    base = f"/files/ids/{ws.pk}"
    crumbs = [("Files", "/files/mine"), ("File IDs", "/files/ids"), (ws.name, base if chain else "")]
    crumbs += [(c.name, f"{base}/f/{c.pk}") for c in chain[:-1]]
    if chain:
        crumbs.append((chain[-1].name, ""))
    access = "Members of this File ID"
    parent = folder
    subs = list(
        Folder.objects.filter(
            context=FolderContext.WORKSPACE, workspace_id=ws.pk, parent=parent, status=FileStatus.ACTIVE
        )
        .select_related("created_by")
        .order_by("name_key")[:500]
    )
    ids = [f.pk for f in subs]
    counts = _folder_counts(ids)
    docs_in = dict(
        FileIdDocument.objects.filter(workspace=ws, folder_id__in=ids, removed_at__isnull=True)
        .values_list("folder_id")
        .annotate(n=Count("pk"))
    )
    items = []
    for f in subs:
        n_folders, _ = counts[f.pk]
        d = folder_item(
            f, (n_folders, docs_in.get(f.pk, 0)), f"{base}/f/{f.pk}", where, access,
            f"{base}/folders/{f.pk}/rename" if can_write else "",
        )  # fmt: skip
        items.append(d)
    docs = (
        ws_sel.active_documents(ws)
        .filter(folder=parent, file__status=FileStatus.ACTIVE)
        .select_related("file__current_version__uploaded_by", "file__current_version__file")
        .order_by("file__display_name")[:500]
    )
    for doc in docs:
        v = doc.file.current_version
        if v is None:
            continue
        d = version_item(v, where, access, mine=False)
        d["can_move"] = can_write
        d["can_hide"] = can_write
        d["hide_url"] = f"{base}/documents/{doc.file_id}/remove"
        items.append(d)
    return {
        "title": folder.name if folder else ws.name, "items": items, "section": "ids", "crumbs": crumbs,
        "scope": "workspace", "workspace_id": str(ws.pk), "can_upload": can_write, "can_create": can_write,
        "create_url": f"{base}/folders" if can_write else "", "create_hidden": [],
        "move_url": f"{base}/move", "actions": [], "next": "",
        "parent_id": str(folder.pk) if folder else "", "show_disabled_create": not can_write,
        "empty": "This folder is empty.", "note": "" if can_write else "You can view and download here.",
        "hide_title": "Remove from this File ID?",
        "hide_text": "The document leaves this File ID. The file and its history are kept.",
        "hide_button": "Remove", "header": {"ws": ws, "role": role, "tab": "files"},
    }  # fmt: skip


def workspace_move_targets(ws: Any) -> list[tuple[str, str]]:
    rows = {
        f["pk"]: f
        for f in Folder.objects.filter(
            context=FolderContext.WORKSPACE, workspace_id=ws.pk, status=FileStatus.ACTIVE
        ).values("pk", "parent_id", "name")[:500]
    }
    out = []
    for r in rows.values():
        parts, cur, guard = [], r, 0
        while cur is not None and guard < 10:
            parts.append(cur["name"])
            cur = rows.get(cur["parent_id"])
            guard += 1
        out.append((str(r["pk"]), f"{ws.name} > " + " > ".join(reversed(parts))))
    return sorted(out, key=lambda t: t[1])
