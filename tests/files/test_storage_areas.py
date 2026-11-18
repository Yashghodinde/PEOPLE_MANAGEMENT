"""IMP-16.3 (STO-01..13): My storage opens on Organized storage and Personal storage; the date logic is untouched;
Personal storage is the person's own tree and never enters the dates."""

import datetime
import uuid

import pytest
from freezegun import freeze_time
from ots.core.errors import ValidationFailed
from ots.files import folders, personal
from ots.files.models import File, FileArea, Folder

from tests.files.conftest import login, upload_all
from tests.files.test_personal import mine_upload

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.usefixtures("dirs")]
DATA = bytes((i * 19) % 256 for i in range(700))


def up_personal(who, root_id, name="2026-11-17_Mine.txt", data=DATA, **extra):
    upload_all(
        login(who), data, uuid.uuid4(), None, name=name, scope="personal", folder_id=str(root_id), **extra
    )
    return File.objects.order_by("-created_at", "-pk").first()


def roots(user):
    return Folder.objects.filter(owner=user, system_key="PERSONAL_ROOT")


def test_my_storage_opens_on_exactly_two_folders(office):
    page = login("emp").get("/files/mine").content.decode()
    assert page.count('data-kind="folder"') == 2
    assert "Organized storage" in page and "Personal storage" in page
    assert "Today" not in page and 'data-kind="file"' not in page
    assert not roots(office["emp"]).exists()  # looking at the root makes nothing


def test_organized_storage_keeps_today_and_the_dates_exactly_as_before(office):
    with freeze_time("2026-11-16 06:00:00"):
        mine_upload("emp", name="2026-11-16_Old-notes.txt")
    with freeze_time("2026-11-17 06:00:00"):
        mine_upload("emp", name="2026-11-17_New-notes.txt", data=DATA + b"1")
        page = login("emp").get("/files/mine/organized").content.decode()
        assert "Today" in page and "17 November 2026" in page and "16 November 2026" in page
        assert page.index("Today") < page.index("16 November 2026")
        assert "Organized storage" in login("emp").get("/files/mine/2026-11-16").content.decode()  # crumbs
        assert "New-notes" in login("emp").get("/files/mine/2026-11-17").content.decode()
    with freeze_time("2026-11-17 18:30:10"):  # midnight in India: yesterday's Today is a dated folder
        page = login("emp").get("/files/mine/organized").content.decode()
        assert "18 November 2026" in page and "17 November 2026" in page


def test_personal_storage_makes_its_root_once_and_only_for_its_owner(office):
    c = login("emp")
    assert c.get("/files/mine/personal").status_code == 200
    assert c.get("/files/mine/personal").status_code == 200
    assert roots(office["emp"]).count() == 1 and not roots(office["sa"]).exists()
    assert folders.personal_root(office["emp"]).pk == roots(office["emp"]).get().pk


def test_a_super_admins_visit_creates_no_folder_for_the_person(office):
    login("boss").get(f"/files/people/{office['emp'].pk}")
    assert not roots(office["emp"]).exists()


def test_create_nested_folders_and_upload_in_personal_storage(office):
    c = login("emp")
    c.get("/files/mine/personal")
    root = roots(office["emp"]).get()
    c.post("/files/mine/folders", {"name": "Finance", "parent": str(root.pk), "next": "/files/mine/personal"})
    fin = Folder.objects.get(name="Finance")
    c.post("/files/mine/folders", {"name": "GST", "parent": str(fin.pk)})
    gst = Folder.objects.get(name="GST")
    assert [x.name for x in folders.ancestors(gst)] == ["Personal storage", "Finance", "GST"]
    f = up_personal("emp", gst.pk)
    assert f.folder_id == gst.pk and f.area == FileArea.PERSONAL
    page = c.get(f"/files/mine/f/{gst.pk}").content.decode()
    assert "Mine.txt" in page and "Personal storage" in page and "data-pagehead" in page
    assert "Organized storage" not in page.split("data-pagehead", 1)[1].split("</nav>", 1)[0]


def test_files_in_personal_storage_never_enter_the_dates_or_the_counts(office):
    mine_upload("emp", name="2026-11-17_Org.txt")
    root = folders.personal_root(office["emp"])
    up_personal("emp", root.pk, name="2026-11-17_Pers.txt", data=DATA + b"2")
    today = login("emp").get(f"/files/mine/{datetime.date.today():%Y-%m-%d}").content.decode()
    assert "Pers.txt" not in today
    assert [v.file.display_name for v in personal.todays_versions(office["emp"])] == ["2026-11-17_Org.txt"]
    assert personal.date_folders(office["emp"])[0] == [] or all(
        n == 1 for _d, n in personal.date_folders(office["emp"])[0]
    )
    assert "Pers.txt" in login("emp").get("/files/mine/personal").content.decode()


def test_a_dropped_file_and_a_folder_upload_land_in_the_open_personal_folder(office):
    root = folders.personal_root(office["emp"])
    f = up_personal(
        "emp", root.pk, name="2026-11-17_Dropped.txt", relative_path="Client/2026/2026-11-17_Dropped.txt"
    )
    assert [x.name for x in folders.ancestors(f.folder)] == ["Personal storage", "Client", "2026"]
    assert f.area == FileArea.PERSONAL


def test_the_fixed_root_cannot_be_renamed_moved_or_nested(office):
    root = folders.personal_root(office["emp"])
    sub = folders.create_personal(office["emp"], "Sub", root.pk)
    with pytest.raises(ValidationFailed):
        folders.rename_personal(office["emp"], root.pk, "Mine")
    with pytest.raises(ValidationFailed):
        folders.move_personal(office["emp"], root.pk, sub.pk)
    assert folders.depth(sub) == 2


def test_moving_between_areas_is_explicit_and_files_follow(office):
    v = mine_upload("emp", name="2026-11-17_Move.txt")
    root = folders.personal_root(office["emp"])
    assert folders.place_personal_file(office["emp"], v.file_id, root.pk).area == FileArea.PERSONAL
    assert File.objects.get(pk=v.file_id).folder_id == root.pk
    back = folders.place_personal_file(
        office["emp"], v.file_id, None
    )  # "the top" of Personal storage is its root
    assert back.folder_id == root.pk and back.area == FileArea.PERSONAL
    org = folders.create_personal(office["emp"], "Org folder")  # a folder under a date
    org_file = mine_upload("emp", name="2026-11-17_InFolder.txt", data=DATA + b"3")
    folders.place_personal_file(office["emp"], org_file.file_id, org.pk)
    folders.move_personal(office["emp"], org.pk, root.pk)  # the whole folder goes to Personal storage
    assert File.objects.get(pk=org_file.file_id).area == FileArea.PERSONAL
    assert (
        Folder.objects.get(pk=org.pk).on_date is None
        and folders.area_of(Folder.objects.get(pk=org.pk)) == FileArea.PERSONAL
    )
    folders.move_personal(
        office["emp"], org.pk, None
    )  # explicit move back "to the top" stays in Personal storage
    assert Folder.objects.get(pk=org.pk).parent_id == root.pk


def test_the_move_dialog_lists_the_personal_root_and_its_folders(office):
    root = folders.personal_root(office["emp"])
    folders.create_personal(office["emp"], "Deep", root.pk)
    page = login("emp").get("/files/mine/personal").content.decode()
    assert "Personal storage &gt; Deep" in page and ">Personal storage</option>" in page


def test_nobody_else_can_open_someones_personal_folders_or_root(office):
    root = folders.personal_root(office["emp"])
    for who in ("sub", "boss", "other"):
        assert login(who).get(f"/files/mine/f/{root.pk}").status_code == 404, who


def test_the_super_admin_sees_the_personal_files_read_only(office):
    root = folders.personal_root(office["emp"])
    up_personal("emp", root.pk, name="2026-11-17_Hidden-gem.txt", data=DATA + b"4")
    page = login("boss").get(f"/files/people/{office['emp'].pk}").content.decode()
    assert "Hidden-gem" in page and "Personal storage" in page and "Download" in page


def test_older_links_and_existing_files_still_work(office):
    v = mine_upload("emp", name="2026-11-17_Old-link.txt")
    day = v.uploaded_on
    assert login("emp").get(f"/files/mine/{day:%Y-%m-%d}").status_code == 200
    assert (
        File.objects.get(pk=v.file_id).area == FileArea.ORGANIZED
    )  # everything that exists stays where it was
