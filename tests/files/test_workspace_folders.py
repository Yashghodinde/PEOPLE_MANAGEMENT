"""IMP-14.6: a File ID is a folder-based workspace on the shared file manager. UI/FID rows."""

import uuid

import pytest
from ots.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from ots.files.models import File, Folder
from ots.workspaces import folders as wf
from ots.workspaces import services as ws_services
from ots.workspaces.models import FileIdDocument
from ots.workspaces.models import Role as WsRole

from tests.files.conftest import login, start, upload_all

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("dirs")]
DATA = bytes((i * 13) % 256 for i in range(800))


@pytest.fixture
def world(office):
    ws = ws_services.create_workspace(office["sub"], name="AZ Exports", file_id="AZ-100")
    ws_services.add_member(office["sub"], ws, office["emp"], WsRole.VIEWER)
    ws_services.add_member(office["sub"], ws, office["other"], WsRole.CONTRIBUTOR)
    return {**office, "ws": ws}


def ws_up(c, ws, name="2026-11-17_Return.txt", data=DATA, **extra):
    upload_all(c, data, uuid.uuid4(), None, name=name, scope="workspace", workspace_id=str(ws.pk), **extra)
    return File.objects.order_by("-created_at", "-pk").first()


def test_the_header_makes_the_file_id_prominent_with_tabs(world):
    page = login("sub").get(f"/files/ids/{world['ws'].pk}").content.decode()
    assert "AZ Exports" in page and "File ID: AZ-100" in page and "Owner:" in page
    assert ">Files</a>" in page and ">Members</a>" in page
    assert "Drop files here, or choose them" not in page  # no big upload panel
    assert "Create or Upload" in page and "Upload folder" in page


def test_the_members_tab_keeps_members_and_details(world):
    page = login("sub").get(f"/files/ids/{world['ws'].pk}?tab=members").content.decode()
    assert "Add a person" in page and "Edit details" in page and "File ID: AZ-100" in page


def test_the_list_is_the_file_manager_with_a_new_file_id_action(world):
    page = login("sub").get("/files/ids").content.decode()
    assert "AZ Exports" in page and "AZ-100" in page and ">New File ID</a>" in page
    assert "Your File IDs" not in page
    assert "New File ID" not in login("emp").get("/files/ids").content.decode()


def test_create_nested_folders_and_crumbs(world):
    c, ws = login("sub"), world["ws"]
    assert c.post(f"/files/ids/{ws.pk}/folders", {"name": "Finance"}).status_code == 302
    fin = Folder.objects.get(name="Finance")
    c.post(f"/files/ids/{ws.pk}/folders", {"name": "GST", "parent": str(fin.pk)})
    gst = Folder.objects.get(name="GST")
    assert gst.parent_id == fin.pk and gst.workspace_id == ws.pk
    page = c.get(f"/files/ids/{ws.pk}/f/{gst.pk}").content.decode()
    assert "data-pagehead" in page and "AZ Exports" in page and ">Finance<" in page and ">GST<" in page


def test_upload_into_a_folder_and_a_folder_upload_keep_the_tree(world):
    ws = world["ws"]
    fol = wf.create(world["sub"], ws, "Returns")
    c = login("sub")
    f = ws_up(c, ws, folder_id=str(fol.pk))
    assert FileIdDocument.objects.get(file=f).folder_id == fol.pk
    g = ws_up(
        c, ws, name="2026-11-17_Deep.txt", data=DATA + b"x", relative_path="Client/2026/2026-11-17_Deep.txt"
    )
    doc = FileIdDocument.objects.get(file=g)
    assert [x.name for x in __import__("ots.files.folders", fromlist=["x"]).ancestors(doc.folder)] == [
        "Client", "2026",
    ]  # fmt: skip


def test_a_viewer_cannot_create_upload_rename_or_move(world):
    ws = world["ws"]
    fol = wf.create(world["sub"], ws, "Returns")
    c = login("emp")
    page = c.get(f"/files/ids/{ws.pk}").content.decode()
    assert "Upload folder" not in page and "New folder" not in page.split("Details")[0]
    assert c.post(f"/files/ids/{ws.pk}/folders", {"name": "X"}).status_code in (302, 403)
    assert not Folder.objects.filter(name="X").exists()
    r = start(c, DATA, uuid.uuid4(), name="2026-11-17_X.txt", scope="workspace", workspace_id=str(ws.pk))
    assert r.status_code == 403
    with pytest.raises(PermissionDenied):
        wf.rename(world["emp"], ws, fol.pk, "Y")


def test_a_folder_of_another_file_id_is_a_404(world):
    other = ws_services.create_workspace(world["sub"], name="Other Co", file_id="OT-200")
    theirs = wf.create(world["sub"], other, "Secret")
    with pytest.raises(NotFound):
        wf.folder_of(world["ws"], theirs.pk)
    assert login("sub").get(f"/files/ids/{world['ws'].pk}/f/{theirs.pk}").status_code == 404


def test_sibling_names_depth_and_moves(world):
    ws, sub = world["ws"], world["sub"]
    a = wf.create(sub, ws, "A")
    with pytest.raises(Conflict):
        wf.create(sub, ws, "a")
    b = wf.create(sub, ws, "B", a.pk)
    with pytest.raises(ValidationFailed):
        wf.move_folder(sub, ws, a.pk, b.pk)  # into its own child
    f = ws_up(login("sub"), ws)
    wf.place_document(sub, ws, f.pk, b.pk)
    assert FileIdDocument.objects.get(file=f).folder_id == b.pk
    wf.place_document(sub, ws, f.pk, None)
    assert FileIdDocument.objects.get(file=f).folder_id is None


def test_an_archived_file_id_changes_nothing(world):
    ws = world["ws"]
    ws.status = "ARCHIVED"
    ws.save(update_fields=["status"])
    with pytest.raises((PermissionDenied, ValidationFailed)):  # archived: nobody contributes
        wf.create(world["sub"], ws, "Late")
