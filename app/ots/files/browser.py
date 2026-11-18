"""What the shared file manager lists (IMP-14.3). One builder per section turns its rows into the same item
dicts, so `templates/files/_manager.html` is written once. Reads only."""

from __future__ import annotations

import datetime
import json
from typing import Any

from django.db.models import Count

from ots.core.clock import IST, today_local

from . import folders as folder_svc
from . import personal
from .models import File, FileStatus, FileVersion, Folder, FolderContext

ACCESS_PERSONAL = "Only you and the Super Admin"


def long_day(d: datetime.date) -> str:
    return f"{d.day} {d:%B %Y}"


def _ext(name: str) -> str:
    return name.rsplit(".", 1)[-1].upper() if "." in name else "FILE"


def folder_item(f: Folder, counts: tuple[int, int], url: str, location: str, access: str) -> dict[str, Any]:
    n_folders, n_files = counts
    count = n_folders + n_files
    d = {
        "kind": "folder", "id": str(f.pk), "name": f.name, "url": url, "type": "Folder",
        "modified": f.updated_at, "by": f.created_by.full_name, "size": None,
        "status": "", "versions": None, "items": count, "location": location, "access": access,
        "created": f.created_at, "folder_id": str(f.pk),
        "can_rename": True, "can_move": True,
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


def personal_day(user: Any, day: datetime.date) -> dict[str, Any]:
    """A date: its root folders, then files uploaded that day that are not in a folder."""
    label = long_day(day)
    fs = _children(user, None, day)
    counts = _folder_counts([f.pk for f in fs])
    items = [folder_item(f, counts[f.pk], f"/files/mine/f/{f.pk}", label, ACCESS_PERSONAL) for f in fs]
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
    items = [folder_item(f, counts[f.pk], f"/files/mine/f/{f.pk}", location, ACCESS_PERSONAL) for f in fs]
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
        "section": "mine", "move_day": day,
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
        "dates": True,
    }  # fmt: skip


def _day_item(d: datetime.date, name: str, n: int | None, now_dt: datetime.datetime) -> dict[str, Any]:
    stamp = datetime.datetime.combine(d, datetime.time(23, 59), tzinfo=IST)
    return {
        "kind": "folder", "id": f"day-{d}", "name": name, "type": "Folder",
        "url": f"/files/mine/{d:%Y-%m-%d}",
        "modified": stamp, "by": "", "size": None, "status": "", "versions": None, "items": n,
        "location": "My storage", "access": ACCESS_PERSONAL, "can_rename": False, "can_move": False,
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


__all__ = ["personal_day", "personal_folder", "personal_root", "move_targets"]
