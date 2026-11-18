"""IMP-14.8 (layout): the Library on the shared file manager. The Super Admin organises and uploads; everyone reads."""

import uuid

import pytest
from ots.files import library
from ots.files.models import File, FileVisibility, LibraryFolder

from tests.files.conftest import login, start, upload_all

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("dirs")]
DATA = bytes((i * 11) % 256 for i in range(900))


def lib_up(c, name="2026-11-17_Policy-notes.txt", data=DATA, **extra):
    upload_all(c, data, uuid.uuid4(), None, name=name, scope="library", **extra)
    return File.objects.order_by("-created_at", "-pk").first()


def test_the_super_admin_uploads_into_the_open_library_folder(office):
    fo = library.create_folder(office["sa"], "Policies")
    f = lib_up(login("boss"), folder_id=str(fo.pk))
    assert f.visibility == FileVisibility.LIBRARY and f.library_folder_id == fo.pk


def test_a_folder_upload_builds_library_folders(office):
    f = lib_up(login("boss"), relative_path="Finance/GST/2026-11-17_Policy-notes.txt")
    assert [x.name for x in (f.library_folder.parent, f.library_folder)] == ["Finance", "GST"]
    assert LibraryFolder.objects.count() == 2


def test_an_employee_cannot_upload_to_the_library(office):
    fo = library.create_folder(office["sa"], "Policies")
    r = start(
        login("emp"), DATA, uuid.uuid4(), name="2026-11-17_X.txt", scope="library", folder_id=str(fo.pk)
    )
    assert r.status_code == 403
    assert not File.objects.exists()


def test_folder_pages_show_the_toolbar_only_to_who_may_use_it(office):
    fo = library.create_folder(office["sa"], "Policies")
    library.create_folder(office["sa"], "Inner", fo.pk)
    boss = login("boss").get(f"/files/library?folder={fo.pk}").content.decode()
    emp = login("emp").get(f"/files/library?folder={fo.pk}").content.decode()
    assert "Create or Upload" in boss and "Upload folder" in boss and "data-pagehead" in boss
    assert "Create or Upload" not in emp and "Upload folder" not in emp and "Inner" in emp


def test_rename_and_move_through_the_manager_forms(office):
    a, b = library.create_folder(office["sa"], "A"), library.create_folder(office["sa"], "B")
    f = lib_up(login("boss"), folder_id=str(a.pk))
    c = login("boss")
    c.post(f"/files/library/folders/{a.pk}/rename", {"name": "Alpha"})
    assert LibraryFolder.objects.get(pk=a.pk).name == "Alpha"
    c.post("/files/library/move", {"kind": "file", "id": str(f.pk), "to": str(b.pk)})
    assert File.objects.get(pk=f.pk).library_folder_id == b.pk
    assert login("emp").post(f"/files/library/folders/{a.pk}/rename", {"name": "x"}).status_code == 403
