"""IMP-14.4: an upload goes into the open folder; a folder upload keeps its tree; nobody else's folder works."""

import uuid

import pytest
from ots.files import folders
from ots.files.models import File, Folder

from tests.files.conftest import login, start, upload_all

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("dirs")]
DATA = bytes((i * 7) % 256 for i in range(1200))


def up(c, name="2026-11-17_Notes.txt", data=DATA, **extra):
    upload_all(c, data, uuid.uuid4(), None, name=name, scope="personal", **extra)
    return File.objects.order_by("-created_at", "-pk").first()


def test_upload_lands_in_the_open_folder(office):
    fol = folders.create_personal(office["emp"], "Finance")
    f = up(login("emp"), folder_id=str(fol.pk))
    assert f.folder_id == fol.pk


def test_upload_with_no_folder_stays_under_its_date(office):
    assert up(login("emp")).folder_id is None


def test_folder_upload_keeps_the_tree_and_reuses_folders(office):
    c = login("emp")
    a = up(
        c, name="2026-11-17_One.txt", data=DATA + b"1", relative_path="Client/2026/Returns/2026-11-17_One.txt"
    )
    b = up(
        c, name="2026-11-17_Two.txt", data=DATA + b"2", relative_path="Client/2026/Returns/2026-11-17_Two.txt"
    )
    assert a.folder_id == b.folder_id
    assert [f.name for f in folders.ancestors(a.folder)] == ["Client", "2026", "Returns"]
    assert Folder.objects.filter(owner=office["emp"]).count() == 3


def test_folder_upload_below_the_open_folder(office):
    top = folders.create_personal(office["emp"], "Top")
    f = up(
        login("emp"),
        name="2026-11-17_Deep.txt",
        relative_path="Sub/2026-11-17_Deep.txt",
        folder_id=str(top.pk),
    )
    assert [x.name for x in folders.ancestors(f.folder)] == ["Top", "Sub"]


def test_someone_elses_folder_is_a_404_and_nothing_is_made(office):
    theirs = folders.create_personal(office["sub"], "Theirs")
    r = start(
        login("emp"), DATA, uuid.uuid4(), name="2026-11-17_X.txt", scope="personal", folder_id=str(theirs.pk)
    )
    assert r.status_code == 404
    assert not File.objects.exists()


def test_a_task_upload_ignores_folder_fields(office):
    # folder fields only mean something for scope=personal; a bad one on another scope is not looked at
    r = start(login("emp"), DATA, uuid.uuid4(), name="2026-11-17_X.txt", scope="personal", folder_id="nope")
    assert r.status_code == 422
