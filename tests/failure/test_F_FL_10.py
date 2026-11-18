"""F-FL-10: across midnight in India nothing moves, duplicates or disappears: yesterday's files appear under
yesterday and Today is empty. There is no midnight job, so there is nothing to miss."""

import datetime
import uuid

import pytest
from freezegun import freeze_time
from ots.files import personal
from ots.files.models import File, FileVersion

from tests.files.conftest import login, upload_all

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("dirs")]
DATA = bytes((i * 9) % 256 for i in range(1200))


def upload(name="2026-11-17_Notes.txt"):
    upload_all(login("emp"), DATA, uuid.uuid4(), None, name=name, scope="personal")


def snapshot(user):
    return sorted(
        FileVersion.objects.values_list("pk", "uploaded_on", "file_id", "uploaded_at")
    ), File.objects.count()


def test_midnight_in_india_moves_a_file_from_today_to_yesterday_without_touching_it(office):
    with freeze_time("2026-11-17 18:29:50"):  # 23:59:50 on the 17th in India
        upload()
        before = snapshot(office["emp"])
        assert [v.file.display_name for v in personal.todays_versions(office["emp"])] == [
            "2026-11-17_Notes.txt"
        ]
        assert personal.date_folders(office["emp"])[0] == []
    with freeze_time("2026-11-17 18:30:10"):  # 00:00:10 on the 18th in India (still the 17th in UTC)
        assert snapshot(office["emp"]) == before  # no row changed, none was added, none removed
        assert personal.todays_versions(office["emp"]) == []
        days, _ = personal.date_folders(office["emp"])
        assert days == [(datetime.date(2026, 11, 17), 1)]
        page = login("emp").get("/files/mine").content.decode()
        assert "Today" in page and "18 November 2026" in page and "17 November 2026" in page
        assert "Notes" not in login("emp").get("/files/mine/2026-11-18").content.decode()  # Today is empty
        assert "2026-11-17_Notes.txt" in login("emp").get("/files/mine/2026-11-17").content.decode()


def test_the_utc_date_change_five_and_a_half_hours_earlier_changes_nothing(office):
    with freeze_time("2026-11-17 23:59:50"):  # 05:29 on the 18th in India
        upload()
    with freeze_time("2026-11-18 00:00:10"):  # UTC midnight: 05:30 on the 18th in India
        assert [v.file.display_name for v in personal.todays_versions(office["emp"])] == [
            "2026-11-17_Notes.txt"
        ]
        assert personal.date_folders(office["emp"])[0] == []


def test_a_new_upload_after_midnight_goes_under_the_new_day_and_the_old_one_stays_put(office):
    with freeze_time("2026-11-17 18:29:50"):
        upload()
    with freeze_time("2026-11-17 18:31:00"):
        upload("2026-11-18_Other-notes.txt")
        today = [v.file.display_name for v in personal.todays_versions(office["emp"])]
        days, _ = personal.date_folders(office["emp"])
    assert today == ["2026-11-18_Other-notes.txt"] and days == [(datetime.date(2026, 11, 17), 1)]
    assert File.objects.count() == 2 and FileVersion.objects.count() == 2


def test_looking_at_the_page_many_times_across_the_boundary_never_writes_anything(office):
    with freeze_time("2026-11-17 18:29:00"):
        upload()
    seen = []
    for stamp in ("2026-11-17 18:29:30", "2026-11-17 18:30:00", "2026-11-17 18:30:01", "2026-11-18 03:00:00"):
        with freeze_time(stamp):
            seen.append(snapshot(office["emp"]))
            assert login("emp").get("/files/mine").status_code == 200
    assert all(s == seen[0] for s in seen)
