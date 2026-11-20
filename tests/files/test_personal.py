"""IMP-02: My storage. Personal uploads, the date folders, rename, hide and restore, and Shared with me."""

import datetime
import uuid

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from freezegun import freeze_time
from ots.accounts.models import Role
from ots.audit.models import AuditLog
from ots.core.clock import today_local
from ots.files import codes, personal, selectors
from ots.files.models import File, FileStatus, FileVersion, FileVisibility, TaskFile

from tests.accounts.factories import make_user
from tests.files.conftest import login, start, upload_all

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("dirs")]
DATA = bytes((i * 3) % 256 for i in range(1500))


def mine_upload(who, name="2026-11-17_Notes.txt", data=DATA, **extra):
    c = login(who)
    upload_all(c, data, uuid.uuid4(), None, name=name, scope="personal", **extra)
    return FileVersion.objects.order_by("-uploaded_at", "-pk").first()


def test_a_personal_upload_makes_a_private_file_owned_by_the_uploader_with_the_pers_token(office):
    v = mine_upload("emp")
    f = v.file
    assert (
        f.visibility == FileVisibility.PRIVATE
        and f.owner_id == office["emp"].pk
        and f.status == FileStatus.ACTIVE
    )
    assert "_PERS-" in v.file_code and "_LIB-" not in v.file_code
    assert not TaskFile.objects.exists()
    assert (
        v.uploaded_on == v.uploaded_at.astimezone(__import__("ots.core.clock", fromlist=["IST"]).IST).date()
    )


def test_the_personal_token_is_only_for_personal_files():
    at = datetime.datetime(2026, 11, 17, 5, 0, tzinfo=datetime.UTC)
    base = {"committed_at": at, "employee_id": "E1", "full_name": "A B", "title": "Notes", "version": 1,
            "original_filename": "a.txt"}  # fmt: skip
    assert "_PERS-notes_" in codes.make_code(task_code=None, place="PERS", **base)
    assert "_LIB-notes_" in codes.make_code(task_code=None, **base)
    assert "_T-1-notes_" in codes.make_code(task_code="T-1", place="PERS", **base)


def test_uploaded_on_is_the_indian_date_not_the_utc_date(office):
    with freeze_time("2026-11-17 18:29:00"):  # 23:59 on the 17th in India
        v1 = mine_upload("emp", name="2026-11-17_Other-notes.txt", data=DATA + b"2")
    with freeze_time("2026-11-17 19:00:00"):  # 00:30 on the 18th in India, still the 17th in UTC
        v2 = mine_upload("emp")
    assert v1.uploaded_on == datetime.date(2026, 11, 17)
    assert v2.uploaded_on == datetime.date(2026, 11, 18)


def test_a_new_version_of_a_personal_file_only_by_its_owner(office):
    v = mine_upload("emp")
    c = login("emp")
    r = start(
        c,
        DATA + b"v2",
        uuid.uuid4(),
        None,
        name="2026-11-17_Notes.txt",
        scope="personal",
        file_id=str(v.file_id),
    )
    assert r.status_code == 201
    other = start(
        login("sub"),
        DATA + b"v3",
        uuid.uuid4(),
        None,
        name="2026-11-17_Notes.txt",
        scope="personal",
        file_id=str(v.file_id),
    )
    assert other.status_code == 422


def test_a_personal_upload_cannot_name_a_task(office):
    r = start(login("emp"), DATA, uuid.uuid4(), office["task"], name="2026-11-17_Notes.txt", scope="personal")
    assert r.status_code == 422


def test_the_name_rule_applies_to_personal_files_too(office, real_name_rule):
    r = start(login("emp"), DATA, uuid.uuid4(), None, name="Untitled.txt", scope="personal")
    assert r.status_code == 422 and "meaningful" in r.json()["message"]


@pytest.mark.parametrize("who", ["boss", "sub", "emp"])
def test_everyone_but_hr_may_use_my_storage(office, who):
    assert login(who).get("/files/mine").status_code == 200


# --- today and the date folders ---------------------------------------------------------------------------------


def test_today_lists_todays_files_and_earlier_days_are_folders_newest_first(office):
    with freeze_time("2026-11-15 06:00:00"):
        mine_upload("emp", name="2026-11-15_Old-notes.txt", data=DATA + b"a")
    with freeze_time("2026-11-16 06:00:00"):
        mine_upload("emp", name="2026-11-16_Mid-notes.txt", data=DATA + b"b")
        mine_upload("emp", name="2026-11-16_More-notes.txt", data=DATA + b"c")
    with freeze_time("2026-11-17 06:00:00"):
        mine_upload("emp", name="2026-11-17_Fresh-notes.txt", data=DATA + b"d")
        c = login("emp")
        root = c.get("/files/mine/organized").content.decode()
        assert "Today" in root and "17 November 2026" in root  # Today is a folder at the top
        assert root.index("16 November 2026") < root.index("15 November 2026")  # earlier dates, newest first
        assert "2 items" in root and "1 item" in root  # each date shows how many files it holds
        today = c.get("/files/mine/2026-11-17").content.decode()
        assert "Fresh-notes" in today and "Mid-notes" not in today
        assert "Mid-notes" in c.get("/files/mine/2026-11-16").content.decode()


def test_a_day_folder_lists_its_files_and_nobody_elses(office):
    with freeze_time("2026-11-16 06:00:00"):
        mine_upload("emp", name="2026-11-16_Esha-notes.txt")
        mine_upload("sub", name="2026-11-16_Sub-secret.txt", data=DATA + b"x")
    with freeze_time("2026-11-17 06:00:00"):
        page = login("emp").get("/files/mine/2026-11-16").content.decode()
    assert "Esha-notes" in page and "Sub-secret" not in page
    assert login("emp").get("/files/mine/not-a-date").status_code == 404


def test_date_folders_page_thirty_at_a_time(office):
    for i in range(35):
        with (
            freeze_time(f"2026-09-{(i % 28) + 1:02d} 06:00:00")
            if i < 28
            else freeze_time(f"2026-10-{i - 27:02d} 06:00:00")
        ):
            mine_upload("emp", name=f"2026-09-01_Day-{i}.txt", data=DATA + str(i).encode())
    with freeze_time("2026-11-17 06:00:00"):
        days, more = personal.date_folders(office["emp"], 0)
        later, more2 = personal.date_folders(office["emp"], 1)
    assert len(days) == 30 and more and len(later) == 5 and not more2
    assert [d for d, _ in days] == sorted((d for d, _ in days), reverse=True)


# --- rename, hide, restore ---------------------------------------------------------------------------------------


def test_rename_changes_only_the_display_name_and_is_audited(office):
    v = mine_upload("emp")
    original = v.original_filename
    r = login("emp").post(f"/files/{v.file_id}/rename", {"name": "2026-11-17_Project-notes.txt"})
    assert r.status_code == 302
    v.refresh_from_db()
    assert v.file.display_name == "2026-11-17_Project-notes.txt" and v.original_filename == original
    assert AuditLog.objects.filter(action="file.renamed").count() == 1


def test_a_rename_to_a_meaningless_name_is_refused(office, real_name_rule):
    v = mine_upload("emp")
    r = login("emp").post(f"/files/{v.file_id}/rename", {"name": "Untitled.txt"})
    assert r.status_code == 422
    v.file.refresh_from_db()
    assert v.file.display_name == "2026-11-17_Notes.txt"


def test_hide_takes_the_file_out_of_the_owners_view_but_removes_nothing(office):
    v = mine_upload("emp")
    c = login("emp")
    assert c.post(f"/files/{v.file_id}/hide").status_code == 302
    f = File.objects.get(pk=v.file_id)
    assert f.status == FileStatus.DELETED and f.deleted_by_id == office["emp"].pk and f.deleted_at
    assert FileVersion.objects.filter(pk=v.pk).exists()  # the version and its blob are untouched
    assert "2026-11-17_Notes.txt" not in c.get("/files/mine").content.decode()
    assert "2026-11-17_Notes.txt" not in c.get("/files/search?q=Notes").content.decode()
    assert c.get(f"/files/v/{v.pk}/download").status_code == 404  # gone from the owner's view
    assert AuditLog.objects.filter(action="file.hidden").count() == 1
    assert c.post(f"/files/{v.file_id}/hide").status_code == 302  # a second press does nothing
    assert AuditLog.objects.filter(action="file.hidden").count() == 1


def test_people_it_was_already_sent_to_keep_it_and_the_super_admin_sees_it_under_deleted(office):
    from ots.files import handover

    v = mine_upload("emp")
    handover.send(office["emp"], v.pk, [office["sub"].pk])
    login("emp").post(f"/files/{v.file_id}/hide")
    assert login("sub").get(f"/files/v/{v.pk}/download").status_code == 200  # access was given and stays
    assert "Notes" in login("sub").get("/files/shared").content.decode()
    deleted = login("boss").get("/files/deleted").content.decode()
    assert "2026-11-17_Notes.txt" in deleted and "Restore" in deleted
    assert login("boss").get(f"/files/v/{v.pk}/download").status_code == 200


def test_only_the_super_admin_restores_and_it_returns_to_the_owner(office):
    v = mine_upload("emp")
    login("emp").post(f"/files/{v.file_id}/hide")
    for who in ("emp", "sub"):
        assert login(who).post(f"/files/{v.file_id}/restore").status_code == 403
        assert login(who).get("/files/deleted").status_code == 403
    assert login("boss").post(f"/files/{v.file_id}/restore").status_code == 302
    assert File.objects.get(pk=v.file_id).status == FileStatus.ACTIVE
    assert "Notes" in login("emp").get(f"/files/mine/{today_local():%Y-%m-%d}").content.decode()
    assert AuditLog.objects.filter(action="file.restored").count() == 1


def test_task_files_and_other_peoples_files_cannot_be_hidden_or_renamed(office):
    upload_all(login("emp"), DATA, uuid.uuid4(), office["task"], name="2026-11-17_Task-file.txt")
    task_file = FileVersion.objects.get()
    assert login("emp").post(f"/files/{task_file.file_id}/hide").status_code == 404
    mine = mine_upload("emp", name="2026-11-17_Private.txt", data=DATA + b"p")
    assert login("sub").post(f"/files/{mine.file_id}/hide").status_code == 404
    assert (
        login("sub").post(f"/files/{mine.file_id}/rename", {"name": "2026-11-17_Other.txt"}).status_code
        == 404
    )
    assert File.objects.get(pk=mine.file_id).status == FileStatus.ACTIVE


def test_shared_with_me_lists_what_others_sent_newest_first(office):
    from ots.files import handover

    a = mine_upload("sub", name="2026-11-17_First-notes.txt")
    b = mine_upload("boss", name="2026-11-17_Second-notes.txt", data=DATA + b"2")
    handover.send(office["sub"], a.pk, [office["emp"].pk])
    handover.send(office["sa"], b.pk, [office["emp"].pk])
    page = login("emp").get("/files/shared").content.decode()
    assert page.index("Second-notes") < page.index("First-notes")
    assert "Boss Person" in page and "Sub Admin" in page and "Shared by" in page


def test_my_storage_pages_have_a_flat_query_count(office):
    for i in range(3):
        mine_upload("emp", name=f"2026-11-17_Small-{i}.txt", data=DATA + str(i).encode())
    c = login("emp")
    with CaptureQueriesContext(connection) as small:
        assert c.get("/files/mine").status_code == 200
    for i in range(3, 25):
        mine_upload("emp", name=f"2026-11-17_Big-{i}.txt", data=DATA + str(i).encode())
    c = login("emp")
    with CaptureQueriesContext(connection) as big:
        assert c.get("/files/mine").status_code == 200
    assert len(big) == len(small)


def test_a_hidden_personal_file_is_gone_from_the_owners_selector_but_not_the_database(office):
    v = mine_upload("emp")
    personal.hide(office["emp"], v.file_id)
    assert not selectors.versions_visible_to(office["emp"]).filter(pk=v.pk).exists()
    assert selectors.versions_visible_to(office["sa"]).filter(pk=v.pk).exists()


def test_hr_has_my_storage_of_their_own_and_nothing_else(office):
    hr = make_user(Role.HR, reports_to=office["sa"], login_id="hrx")
    c = login("hrx")
    assert c.get("/files/mine").status_code == 200
    assert c.get("/files/mine/personal").status_code == 200
    emp_v = mine_upload("emp", name="2026-11-17_Emp-only.txt")
    v = mine_upload("hrx", name="2026-11-17_Hr-notes.txt")
    assert v.uploaded_by_id == hr.pk and v.file.owner_id == hr.pk
    assert list(selectors.versions_visible_to(hr)) == [v]  # their own file, never the employee's
    assert not selectors.versions_visible_to(office["emp"]).filter(pk=v.pk).exists()
    assert selectors.versions_visible_to(office["sa"]).filter(pk=v.pk).exists()
    assert c.get(f"/files/v/{emp_v.pk}/download").status_code in (403, 404)
    for path in (
        "/files/tasks",
        "/files/library",
        "/files/shared",
        "/files/ids",
        "/files/people",
        "/files/deleted",
    ):
        assert c.get(path).status_code in (403, 404), path
