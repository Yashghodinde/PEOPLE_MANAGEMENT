"""IMP-15.4: physical records of a File ID (FID-08..12): numbers, independent places, history that cannot change."""

import uuid

import pytest
from django.db import connection
from django.db.utils import DatabaseError
from ots.audit.models import AuditLog
from ots.core.errors import NotFound, PermissionDenied, ValidationFailed
from ots.workspaces import physical
from ots.workspaces import services as ws_services
from ots.workspaces.models import FileIdDocument, PhysicalMove, PhysicalRecord, PhysicalStatus
from ots.workspaces.models import Role as WsRole

from tests.files.conftest import login, upload_all

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("dirs")]


@pytest.fixture
def world(office):
    ws = ws_services.create_workspace(office["sub"], name="AZ Exports")
    ws_services.add_member(office["sub"], ws, office["emp"], WsRole.VIEWER)
    return {**office, "ws": ws}


def add(w, name="GST documents", **kw):
    args = {"office": "Mumbai", "block": "B-02", "shelf": "S-04", "box": "BX-12"} | kw
    return physical.create(w["sub"], w["ws"], name=name, **args)


def test_a_record_gets_a_generated_number_and_a_readable_place(world):
    a, b = add(world), add(world, "Agreements", office="Kerala", block="B-01", shelf="S-02", box="")
    assert a.number.startswith("PHY-") and len(a.number) == 10 and a.number != b.number
    assert a.place == "Mumbai > Block B-02 > Shelf S-04 > Box BX-12"
    assert b.place == "Kerala > Block B-01 > Shelf S-02"
    assert AuditLog.objects.filter(action="physical.created").count() == 2


def test_one_file_id_keeps_many_records_in_different_places(world):
    add(world, "Registration", shelf="S-04")
    add(world, "GST", shelf="S-05")
    add(world, "Agreements", office="Kerala", block="B-01", shelf="S-02")
    assert {r.shelf for r in PhysicalRecord.objects.filter(workspace=world["ws"])} == {"S-04", "S-05", "S-02"}


def test_the_first_history_row_is_the_initial_storage_and_a_move_keeps_the_old_place(world):
    rec = add(world)
    first = PhysicalMove.objects.get(record=rec)
    assert first.from_office == "" and first.to_shelf == "S-04" and first.reason == "Initial storage"
    physical.move(
        world["sub"], world["ws"], rec.pk, office="Mumbai", block="B-03", shelf="S-09", reason="Audit"
    )
    rec.refresh_from_db()
    assert rec.place == "Mumbai > Block B-03 > Shelf S-09"
    last = PhysicalMove.objects.filter(record=rec).latest("id")
    assert (last.from_block, last.from_shelf, last.to_block, last.to_shelf) == (
        "B-02",
        "S-04",
        "B-03",
        "S-09",
    )
    assert PhysicalMove.objects.filter(record=rec).count() == 2 and last.reason == "Audit"


def test_a_move_to_the_same_place_is_refused(world):
    rec = add(world)
    with pytest.raises(ValidationFailed):
        physical.move(
            world["sub"], world["ws"], rec.pk, office="Mumbai", block="B-02", shelf="S-04", box="BX-12"
        )


def test_location_history_cannot_be_edited_or_deleted_even_by_sql(world):
    rec = add(world)
    row = PhysicalMove.objects.get(record=rec)
    with connection.cursor() as cur:
        with pytest.raises(DatabaseError):
            cur.execute("UPDATE physical_moves SET to_shelf = 'X' WHERE id = %s", [row.pk])
    with connection.cursor() as cur:
        with pytest.raises(DatabaseError):
            cur.execute("DELETE FROM physical_moves WHERE id = %s", [row.pk])


def test_a_record_may_exist_without_a_digital_file_or_link_one_of_this_file_id(world):
    plain = add(world)
    assert plain.file_id is None
    upload_all(login("sub"), b"x" * 900, uuid.uuid4(), None, name="2026-11-17_Scan.txt", scope="workspace",
               workspace_id=str(world["ws"].pk))  # fmt: skip
    doc = FileIdDocument.objects.get(workspace=world["ws"])
    linked = add(world, "With scan", file_id=doc.file_id)
    assert linked.file_id == doc.file_id
    other = ws_services.create_workspace(world["sub"], name="Other Co")
    with pytest.raises(ValidationFailed):  # a file that is not in this File ID cannot be linked
        physical.create(world["sub"], other, name="X", office="M", block="B", shelf="S", file_id=doc.file_id)


def test_required_fields(world):
    for missing in ("name", "office", "block", "shelf"):
        kw = {"name": "N", "office": "O", "block": "B", "shelf": "S"} | {missing: ""}
        with pytest.raises(ValidationFailed):
            physical.create(world["sub"], world["ws"], **kw)


def test_a_viewer_sees_but_cannot_change_and_the_owner_can_hide_them_from_viewers(world):
    rec = add(world)
    assert login("emp").get(f"/files/ids/{world['ws'].pk}/physical").status_code == 200
    with pytest.raises(PermissionDenied):
        physical.move(world["emp"], world["ws"], rec.pk, office="X", block="B", shelf="S")
    login("emp").post(
        f"/files/ids/{world['ws'].pk}/physical/new", {"name": "n", "office": "o", "block": "b", "shelf": "s"}
    )
    assert PhysicalRecord.objects.count() == 1
    world["ws"].physical_visible_to_viewers = False
    world["ws"].save(update_fields=["physical_visible_to_viewers"])
    assert login("emp").get(f"/files/ids/{world['ws'].pk}/physical").status_code == 404
    assert b"Physical records" not in login("emp").get(f"/files/ids/{world['ws'].pk}").content


def test_a_stranger_gets_the_same_404_for_the_page_and_the_actions(world):
    rec = add(world)
    c = login("other")
    base = f"/files/ids/{world['ws'].pk}/physical"
    assert c.get(base).status_code == 404
    assert c.post(f"{base}/new", {"name": "n", "office": "o", "block": "b", "shelf": "s"}).status_code == 404
    assert c.post(f"{base}/{rec.pk}/move", {"office": "o", "block": "b", "shelf": "s"}).status_code == 404
    with pytest.raises(NotFound):
        physical.record_of(world["other"], world["ws"], rec.pk)


def test_status_changes_are_audited_and_a_move_finds_a_missing_record(world):
    rec = add(world)
    physical.set_status(world["sub"], world["ws"], rec.pk, PhysicalStatus.MISSING)
    assert AuditLog.objects.filter(action="physical.status").count() == 1
    physical.move(world["sub"], world["ws"], rec.pk, office="Mumbai", block="B-09", shelf="S-01")
    rec.refresh_from_db()
    assert rec.status == PhysicalStatus.STORED
    with pytest.raises(ValidationFailed):
        physical.set_status(world["sub"], world["ws"], rec.pk, "LOST")


def test_the_page_shows_cards_with_location_status_and_history(world):
    rec = add(world)
    physical.move(world["sub"], world["ws"], rec.pk, office="Mumbai", block="B-03", shelf="S-09")
    page = login("sub").get(f"/files/ids/{world['ws'].pk}/physical").content.decode()
    for text in (
        rec.number,
        "Mumbai &gt; Block B-03 &gt; Shelf S-09",
        "Stored",
        "Where it has been",
        "Add a physical record",
    ):
        assert text in page, text
    assert page.index("Moved to") < page.index("Stored at")  # newest first in the history
