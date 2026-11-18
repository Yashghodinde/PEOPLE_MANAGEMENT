"""Logical folders (IMP-14.2): create, rename, move, place a file. Writes only; all audited.

Personal folders belong to their owner; a stranger's folder id is a 404. The date
level above the roots is virtual, so a root folder carries the date it was made on and stays under it.
"""

from __future__ import annotations

import datetime
from typing import Any

from django.db import IntegrityError, transaction
from django.http import HttpRequest

from ots.audit import api as audit
from ots.core import events
from ots.core.clock import now, today_local
from ots.core.errors import Conflict, NotFound, ValidationFailed
from ots.core.permissions import require

from .models import File, FileArea, FileStatus, FileVisibility, Folder, FolderContext

NAME_MAX = 120
MAX_DEPTH = 8
PERSONAL_ROOT = "PERSONAL_ROOT"
PERSONAL_ROOT_NAME = "Personal storage"


def clean_name(raw: str) -> str:
    name = " ".join((raw or "").split())
    if not name or len(name) > NAME_MAX or "/" in name or "\\" in name or name in {".", ".."}:
        raise ValidationFailed(
            "Give the folder a name of up to 120 characters, without slashes.", fields={"name": ["Invalid."]}
        )
    return name


def depth(folder: Folder) -> int:
    """1 for a root folder."""
    n, seen, cur = 1, {folder.pk}, folder
    while cur.parent_id is not None:
        cur = Folder.objects.get(pk=cur.parent_id)
        if cur.pk in seen:  # a loop can never be saved, but never spin on one
            break
        seen.add(cur.pk)
        n += 1
    return n


def ancestors(folder: Folder) -> list[Folder]:
    """Root first, ending with the folder itself."""
    chain, cur = [folder], folder
    while cur.parent_id is not None and len(chain) <= MAX_DEPTH + 1:
        cur = Folder.objects.get(pk=cur.parent_id)
        chain.append(cur)
    return chain[::-1]


def root_date(folder: Folder) -> datetime.date:
    return ancestors(folder)[0].on_date or today_local()


def my_folder(user: Any, folder_id: Any) -> Folder:
    f = Folder.objects.filter(
        pk=folder_id, context=FolderContext.PERSONAL, owner=user, status=FileStatus.ACTIVE
    ).first()
    if f is None:
        raise NotFound("Folder not found.")  # someone else's folder and a missing one look the same
    return f


def create_personal(
    user: Any,
    name: str,
    parent_id: Any = None,
    on_date: datetime.date | None = None,
    request: HttpRequest | None = None,
) -> Folder:
    """A folder inside the open one; with no parent it is a root folder under `on_date` (default today)."""
    require(user, "file.upload_personal")
    name = clean_name(name)
    parent = my_folder(user, parent_id) if parent_id else None
    if parent is not None and depth(parent) >= MAX_DEPTH:
        raise ValidationFailed(
            f"Folders can go {MAX_DEPTH} levels deep, no further.", fields={"name": ["Too deep."]}
        )
    day = None if parent is not None else (on_date or today_local())
    try:
        with transaction.atomic():
            folder = Folder.objects.create(
                context=FolderContext.PERSONAL, owner=user, parent=parent, name=name,
                name_key=name.lower(), on_date=day, created_by=user,
            )  # fmt: skip
            audit.record(
                "folder.created", user, folder, {"name": name, "parent": str(parent_id or "")}, request
            )
            events.publish("folder.changed", ("folder", str(folder.pk)), {"name": name})
    except IntegrityError:
        raise Conflict("A folder with that name already exists here.") from None
    return folder


def ensure_path(
    user: Any, parts: list[str], parent_id: Any = None, on_date: datetime.date | None = None
) -> Folder | None:
    """Create (or reuse) each folder in `parts` below `parent_id` (a folder upload). Returns the last."""
    cur_id = parent_id
    last: Folder | None = None
    for raw in parts:
        name = clean_name(raw)
        parent = my_folder(user, cur_id) if cur_id else None
        existing = Folder.objects.filter(
            context=FolderContext.PERSONAL, owner=user, parent=parent, name_key=name.lower(),
            on_date=None if parent else (on_date or today_local()), status=FileStatus.ACTIVE,
        ).first()  # fmt: skip
        last = existing or create_personal(user, name, cur_id, on_date)
        cur_id = last.pk
    return last


@transaction.atomic
def rename_personal(user: Any, folder_id: Any, name: str, request: HttpRequest | None = None) -> Folder:
    folder = Folder.objects.select_for_update().filter(pk=folder_id, owner=user).first()
    if folder is None or folder.context != FolderContext.PERSONAL or folder.status != FileStatus.ACTIVE:
        raise NotFound("Folder not found.")
    if folder.system_key:
        raise ValidationFailed("This folder is fixed and cannot be renamed.", fields={"name": ["Fixed."]})
    name = clean_name(name)
    if name.lower() == folder.name_key and name == folder.name:
        return folder
    old = folder.name
    folder.name, folder.name_key, folder.updated_at = name, name.lower(), now()
    try:
        with transaction.atomic():
            folder.save(update_fields=["name", "name_key", "updated_at"])
    except IntegrityError:
        raise Conflict("A folder with that name already exists here.") from None
    audit.record("folder.renamed", user, folder, {"from": old, "to": name}, request)
    return folder


@transaction.atomic
def move_personal(user: Any, folder_id: Any, to_parent_id: Any, request: HttpRequest | None = None) -> Folder:
    """Move a folder into another of the person's own folders, or to a root (empty, under today)."""
    folder = my_folder(user, folder_id)
    if folder.system_key:
        raise ValidationFailed("This folder is fixed and cannot be moved.", fields={"to": ["Fixed."]})
    here = area_of(folder)
    parent = my_folder(user, to_parent_id) if to_parent_id else None
    if parent is None and here == FileArea.PERSONAL:
        parent = personal_root(user)  # "the top" of Personal storage is its root, not a date
    if parent is not None:
        if folder.pk in {a.pk for a in ancestors(parent)}:
            raise ValidationFailed("A folder cannot go inside itself.", fields={"to": ["Invalid."]})
        # the deepest branch under `folder` plus the new parent's depth must stay within the cap
        if depth(parent) + height(folder) > MAX_DEPTH:
            raise ValidationFailed(
                f"Folders can go {MAX_DEPTH} levels deep, no further.", fields={"to": ["Too deep."]}
            )
    old = str(folder.parent_id or "")
    folder.parent = parent
    folder.on_date = None if (parent is not None) else today_local()
    folder.updated_at = now()
    try:
        with transaction.atomic():
            folder.save(update_fields=["parent", "on_date", "updated_at"])
    except IntegrityError:
        raise Conflict("A folder with that name already exists there.") from None
    new_area = area_of(folder)
    if new_area != here:
        _set_area(folder, new_area)
    audit.record("folder.moved", user, folder, {"from": old, "to": str(to_parent_id or "")}, request)
    return folder


def height(folder: Folder) -> int:
    kids = list(Folder.objects.filter(parent=folder, status=FileStatus.ACTIVE).values_list("pk", flat=True))
    return 1 + max((height(Folder.objects.get(pk=k)) for k in kids), default=0)


@transaction.atomic
def place_personal_file(user: Any, file_id: Any, folder_id: Any, request: HttpRequest | None = None) -> File:
    """Put one of the person's own private files in a folder (empty = directly under its date)."""
    f = File.objects.select_for_update().filter(pk=file_id, visibility=FileVisibility.PRIVATE).first()
    if f is None or f.owner_id != user.pk or f.status != FileStatus.ACTIVE:
        raise NotFound("File not found.")
    target = my_folder(user, folder_id) if folder_id else None
    if target is None and f.area == FileArea.PERSONAL:
        target = personal_root(user)  # the top of Personal storage is its root
    if f.folder_id == (target.pk if target else None):
        return f
    old = str(f.folder_id or "")
    f.folder = target
    f.area = area_of(target)
    f.save(update_fields=["folder", "area"])
    audit.record("file.moved", user, f, {"from": old, "to": str(folder_id or "")}, request)
    return f


# ---------------------------------------------------------------- the two areas of My storage (IMP-16.3)


def personal_root(user: Any) -> Folder:
    """The fixed root of a person's Personal storage, made the first time it is needed (never by someone
    else's visit). One per person: the folder table's unique constraint refuses a second."""
    root = Folder.objects.filter(
        context=FolderContext.PERSONAL, owner=user, system_key=PERSONAL_ROOT, status=FileStatus.ACTIVE
    ).first()
    if root is not None:
        return root
    try:
        with transaction.atomic():
            return Folder.objects.create(
                context=FolderContext.PERSONAL, owner=user, parent=None, name=PERSONAL_ROOT_NAME,
                name_key=PERSONAL_ROOT_NAME.lower(), on_date=None, system_key=PERSONAL_ROOT, created_by=user,
            )  # fmt: skip
    except IntegrityError:  # two requests at once: the other one made it
        return Folder.objects.get(
            context=FolderContext.PERSONAL, owner=user, system_key=PERSONAL_ROOT, status=FileStatus.ACTIVE
        )


def area_of(folder: Folder | None) -> str:
    """PERSONAL inside Personal storage, ORGANIZED otherwise (no folder = loose under its date)."""
    if folder is None:
        return FileArea.ORGANIZED
    return FileArea.PERSONAL if ancestors(folder)[0].system_key == PERSONAL_ROOT else FileArea.ORGANIZED


def area_of_id(folder_id: Any) -> str:
    f = Folder.objects.filter(pk=folder_id).first() if folder_id else None
    return area_of(f)


def subtree_ids(folder: Folder) -> list[Any]:
    """The folder and every folder below it."""
    found, frontier = [folder.pk], [folder.pk]
    while frontier and len(found) < 5000:
        kids = list(Folder.objects.filter(parent_id__in=frontier).values_list("pk", flat=True))
        found += kids
        frontier = kids
    return found


def _set_area(folder: Folder, area: str) -> None:
    """Files inside a moved folder follow it into the other area (nothing is copied)."""
    File.objects.filter(folder_id__in=subtree_ids(folder)).exclude(area=area).update(area=area)
