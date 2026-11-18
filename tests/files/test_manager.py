"""IMP-14.3: My storage on the shared file manager. UI-01..04, UI-15/16, FILE-01/02/06."""

import pytest
from freezegun import freeze_time
from ots.accounts.models import Role
from ots.core.clock import today_local
from ots.files import folders
from ots.files.models import File, Folder

from tests.accounts.factories import make_user
from tests.files.conftest import login
from tests.files.test_personal import mine_upload

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("dirs")]


def today_url():
    return f"/files/mine/{today_local():%Y-%m-%d}"


def test_my_storage_is_a_folder_list_with_no_big_upload_card(office):
    page = login("emp").get("/files/mine").content.decode()
    assert "Create or Upload" in page and "Organized storage" in page and "Personal storage" in page
    assert "Today" not in page  # the date folders live one level down, inside Organized storage
    assert "Today" in login("emp").get("/files/mine/organized").content.decode()
    assert "Drop files here, or choose them" not in page and "Choose files</button>" not in page
    assert "Earlier days" not in page and "No earlier files" not in page
    assert "Only you and the Super Admin can see these files" not in page  # no explanatory paragraph


def test_the_menu_has_every_create_and_upload_action_inside_a_folder_view(office):
    page = login("emp").get(today_url()).content.decode()
    for text in ("New folder", "Upload files", "Upload folder", "webkitdirectory", "Drop to upload here"):
        assert text in page, text
    assert 'for="fm-sort"' in page and ">List</button>" in page and ">Grid</button>" in page
    assert "Details</button>" in page


def test_a_new_folder_appears_inside_the_open_folder_and_the_crumbs_show_the_path(office):
    c = login("emp")
    assert c.post("/files/mine/folders", {"name": "Finance", "next": today_url()}).status_code == 302
    fin = Folder.objects.get(name="Finance")
    assert (
        c.post(
            "/files/mine/folders", {"name": "GST", "parent": str(fin.pk), "next": f"/files/mine/f/{fin.pk}"}
        ).status_code
        == 302
    )
    gst = Folder.objects.get(name="GST")
    assert gst.parent_id == fin.pk
    page = c.get(f"/files/mine/f/{gst.pk}").content.decode()
    assert "data-pagehead" in page and f'href="/files/mine/f/{fin.pk}"' in page
    assert 'aria-current="page"' in page and ">GST<" in page and "This folder is empty." in page
    assert (
        "New folder" in page and "Upload files" in page
    )  # an empty folder keeps the toolbar and the drop area


def test_a_duplicate_name_comes_back_as_a_message_not_a_dead_end(office):
    c = login("emp")
    c.post("/files/mine/folders", {"name": "Finance", "next": today_url()})
    r = c.post("/files/mine/folders", {"name": "finance", "next": today_url()}, follow=True)
    assert r.status_code == 200 and "already exists" in r.content.decode()
    assert Folder.objects.filter(owner=office["emp"]).count() == 1


def test_a_past_date_cannot_take_new_files_and_says_so(office):
    with freeze_time("2026-11-16 06:00:00"):
        mine_upload("emp", name="2026-11-16_Old-notes.txt")
    page = login("emp").get("/files/mine/2026-11-16").content.decode()
    assert "Old-notes" in page and "disabled" in page and "New files go to Today" in page
    assert "data-locked" in page


def test_folder_contents_show_the_file_and_the_root_does_not(office):
    v = mine_upload("emp", name="2026-11-17_Work.txt")
    fol = folders.create_personal(office["emp"], "Docs")
    folders.place_personal_file(office["emp"], v.file_id, fol.pk)
    c = login("emp")
    assert "Work.txt" not in c.get(today_url()).content.decode()
    assert "Work.txt" in c.get(f"/files/mine/f/{fol.pk}").content.decode()
    assert "1 item" in c.get(today_url()).content.decode()


def test_move_a_file_and_a_folder_through_the_form(office):
    v = mine_upload("emp", name="2026-11-17_Work.txt")
    a, b = folders.create_personal(office["emp"], "A"), folders.create_personal(office["emp"], "B")
    c = login("emp")
    c.post("/files/mine/move", {"kind": "file", "id": str(v.file_id), "to": str(a.pk)})
    assert File.objects.get(pk=v.file_id).folder_id == a.pk
    c.post("/files/mine/move", {"kind": "folder", "id": str(b.pk), "to": str(a.pk)})
    assert Folder.objects.get(pk=b.pk).parent_id == a.pk


def test_someone_elses_folder_is_a_404_for_every_route(office):
    theirs = folders.create_personal(office["sub"], "Theirs")
    c = login("emp")
    assert c.get(f"/files/mine/f/{theirs.pk}").status_code == 404
    c.post(f"/files/mine/folders/{theirs.pk}/rename", {"name": "mine now"})
    assert Folder.objects.get(pk=theirs.pk).name == "Theirs"
    assert not login("boss").get(f"/files/mine/f/{theirs.pk}").status_code == 200


def test_hr_has_no_storage_pages(office):
    make_user(Role.HR, reports_to=office["sa"], login_id="hrz")
    assert login("hrz").get("/files/mine").status_code in (302, 403, 404)


def test_a_folder_opens_from_anywhere_on_its_card_and_a_file_does_not(office):
    """IMP-15.1: one stretched link per folder (no nested interactive elements); files keep their own actions."""
    v = mine_upload("emp", name="2026-11-17_Work.txt")
    fol = folders.create_personal(office["emp"], "Docs")
    page = login("emp").get(today_url()).content.decode()
    items = page.split('<li class="fm-item"')[1:]
    folder_li = next(i for i in items if 'data-kind="folder"' in i.split(">", 1)[0])
    file_li = next(i for i in items if 'data-kind="file"' in i.split(">", 1)[0])
    assert folder_li.count("fm-stretch") == 1 and f"/files/mine/f/{fol.pk}" in folder_li
    assert "fm-stretch" not in file_li and f"/files/v/{v.pk}/download" in file_li
    # the stretched link has no interactive element inside it
    link = folder_li.split('class="fm-link fm-stretch">', 1)[1].split("</a>", 1)[0]
    assert "<" not in link
    assert 'hx-boost="false"' in file_li  # a download is never swapped into the page


def test_both_layouts_exist_with_the_labels_and_the_pinned_actions(office):
    mine_upload("emp", name="2026-11-17_Work.txt")
    page = login("emp").get(today_url()).content.decode()
    for needle in (
        'data-label="Modified"',
        'data-label="Modified by"',
        'data-label="Size"',
        "fm-actions",
        "fm-status",
    ):
        assert needle in page, needle
