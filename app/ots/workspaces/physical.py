"""Physical records of a File ID (IMP-15.4): where the hard copies are kept. Writes only; each is audited.

Owner and Contributor add, move and mark records; every move is a new row in `PhysicalMove` (never edited or
removed, the database refuses it). A Viewer sees the records unless the Owner turned that off. Someone outside
the File ID gets the same 404 as for a File ID that does not exist.
"""

from __future__ import annotations

from typing import Any

from django.db import connection, transaction
from django.db.models import QuerySet
from django.http import HttpRequest

from ots.audit import api as audit
from ots.core.errors import NotFound, ValidationFailed
from ots.core.permissions import require

from .models import (
    FileIdDocument,
    FileIdWorkspace,
    PhysicalMove,
    PhysicalRecord,
    PhysicalStatus,
    Role,
    WorkspaceStatus,
)
from .permissions import effective_role

LIMITS = {"name": 200, "category": 60, "office": 80, "block": 40, "shelf": 40, "box": 40, "notes": 2000}


def _text(raw: Any, field: str, *, required: bool = False) -> str:
    value = " ".join(str(raw or "").split())[: LIMITS[field]]
    if required and not value:
        raise ValidationFailed(f"Fill in the {field}.", fields={field: ["Required."]})
    return value


def _writable(user: Any, ws: FileIdWorkspace) -> None:
    require(user, "fileid.contribute", ws)
    if ws.status == WorkspaceStatus.ARCHIVED:
        raise ValidationFailed("This File ID is archived. Restore it to change anything.")


def can_see(user: Any, ws: FileIdWorkspace) -> bool:
    role = effective_role(user, ws)
    if role is None:
        return False
    return role != Role.VIEWER or ws.physical_visible_to_viewers


def records(user: Any, ws: FileIdWorkspace) -> QuerySet[PhysicalRecord]:
    if not can_see(user, ws):
        return PhysicalRecord.objects.none()
    return ws.physical_records.select_related("file", "stored_by").order_by("-stored_at", "number")


def record_of(user: Any, ws: FileIdWorkspace, record_id: Any) -> PhysicalRecord:
    rec = records(user, ws).filter(pk=record_id).first()
    if rec is None:
        raise NotFound("Not found.")
    return rec


def suggestions(ws: FileIdWorkspace) -> dict[str, list[str]]:
    """Offices, blocks and shelves already used in this File ID, to pick from or to type over."""
    qs = ws.physical_records
    return {k: sorted(set(qs.values_list(k, flat=True)) - {""}) for k in ("office", "block", "shelf", "box")}


def _file_of(ws: FileIdWorkspace, file_id: Any) -> Any:
    if not file_id:
        return None
    doc = (
        FileIdDocument.objects.select_related("file")
        .filter(workspace=ws, file_id=file_id, removed_at__isnull=True)
        .first()
    )
    if doc is None:
        raise ValidationFailed("Pick a document of this File ID.", fields={"file": ["Invalid."]})
    return doc.file


def _next_number() -> str:
    with connection.cursor() as cur:
        cur.execute("SELECT nextval('physical_number_seq')")
        return f"PHY-{cur.fetchone()[0]:06d}"


@transaction.atomic
def create(
    user: Any, ws: FileIdWorkspace, *, name: str, office: str, block: str, shelf: str, box: str = "",
    category: str = "", notes: str = "", file_id: Any = None, request: HttpRequest | None = None,
) -> PhysicalRecord:  # fmt: skip
    _writable(user, ws)
    rec = PhysicalRecord.objects.create(
        number=_next_number(), workspace=ws, name=_text(name, "name", required=True),
        category=_text(category, "category"), office=_text(office, "office", required=True),
        block=_text(block, "block", required=True), shelf=_text(shelf, "shelf", required=True),
        box=_text(box, "box"), notes=str(notes or "").strip()[: LIMITS["notes"]], stored_by=user,
        file=_file_of(ws, file_id),
    )  # fmt: skip
    PhysicalMove.objects.create(
        record=rec, to_office=rec.office, to_block=rec.block, to_shelf=rec.shelf, to_box=rec.box,
        changed_by=user, reason="Initial storage",
    )  # fmt: skip
    audit.record("physical.created", user, rec, {"number": rec.number, "place": rec.place}, request)
    return rec


@transaction.atomic
def move(
    user: Any, ws: FileIdWorkspace, record_id: Any, *, office: str, block: str, shelf: str, box: str = "",
    reason: str = "", request: HttpRequest | None = None,
) -> PhysicalRecord:  # fmt: skip
    _writable(user, ws)
    rec = PhysicalRecord.objects.select_for_update().filter(pk=record_id, workspace=ws).first()
    if rec is None or not can_see(user, ws):
        raise NotFound("Not found.")
    new = (
        _text(office, "office", required=True), _text(block, "block", required=True),
        _text(shelf, "shelf", required=True), _text(box, "box"),
    )  # fmt: skip
    if new == (rec.office, rec.block, rec.shelf, rec.box):
        raise ValidationFailed("That is where it already is.", fields={"shelf": ["Unchanged."]})
    PhysicalMove.objects.create(
        record=rec, from_office=rec.office, from_block=rec.block, from_shelf=rec.shelf, from_box=rec.box,
        to_office=new[0], to_block=new[1], to_shelf=new[2], to_box=new[3], changed_by=user,
        reason=" ".join((reason or "").split())[:200],
    )  # fmt: skip
    old = rec.place
    rec.office, rec.block, rec.shelf, rec.box = new
    if rec.status == PhysicalStatus.MISSING:  # it was found: it is where it was just put
        rec.status = PhysicalStatus.STORED
    rec.save(update_fields=["office", "block", "shelf", "box", "status"])
    audit.record("physical.moved", user, rec, {"from": old, "to": rec.place}, request)
    return rec


@transaction.atomic
def set_status(
    user: Any, ws: FileIdWorkspace, record_id: Any, status: str, request: HttpRequest | None = None
) -> PhysicalRecord:
    _writable(user, ws)
    if status not in PhysicalStatus.values:
        raise ValidationFailed("Choose a status.", fields={"status": ["Invalid."]})
    rec = PhysicalRecord.objects.select_for_update().filter(pk=record_id, workspace=ws).first()
    if rec is None or not can_see(user, ws):
        raise NotFound("Not found.")
    if rec.status != status:
        old, rec.status = rec.status, status
        rec.save(update_fields=["status"])
        audit.record("physical.status", user, rec, {"from": old, "to": status}, request)
    return rec
