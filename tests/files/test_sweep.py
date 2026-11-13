"""M08.2: the staging sweep (an upload with no piece for 24 hours is abandoned)."""

import datetime
import os
import uuid

import pytest
from freezegun import freeze_time
from ots.core.clock import now
from ots.core.jobs import registered_jobs
from ots.files import layout, services
from ots.files.models import FileState, UploadSession

from tests.files.conftest import login, put, start

pytestmark = pytest.mark.django_db
DATA = bytes(range(256)) * 12


def test_an_upload_idle_for_24_hours_is_abandoned_and_its_part_deleted(office):
    c = login("emp")
    with freeze_time("2026-11-13 08:00:00"):
        uid = start(c, DATA, uuid.uuid4(), office["task"]).json()["upload_id"]
        put(c, uid, DATA, 0, 1023)
    with freeze_time("2026-11-14 07:59:00"):  # 23 h 59 min later
        assert services.staging_sweep() == {"abandoned": 0, "stray_parts": 0}
        assert layout.staging_path(uid).exists()
    with freeze_time("2026-11-14 08:01:00"):
        assert services.staging_sweep() == {"abandoned": 1, "stray_parts": 0}
    assert not layout.staging_path(uid).exists()
    assert UploadSession.objects.get().state == FileState.ABANDONED


def test_a_piece_resets_the_clock(office):
    c = login("emp")
    with freeze_time("2026-11-13 08:00:00"):
        uid = start(c, DATA, uuid.uuid4(), office["task"]).json()["upload_id"]
    with freeze_time("2026-11-14 06:00:00"):
        c = login("emp")  # a fresh browser session: the first one is old by now
        assert put(c, uid, DATA, 0, 1023).status_code == 200
    with freeze_time("2026-11-14 12:00:00"):
        assert services.staging_sweep()["abandoned"] == 0


def test_finished_and_rejected_uploads_are_left_alone(office):
    c = login("emp")
    uid = start(c, DATA, uuid.uuid4(), office["task"]).json()["upload_id"]
    for a in (0, 1024, 2048):
        put(c, uid, DATA, a, a + 1023)
    later = now() + datetime.timedelta(days=3)
    assert services.staging_sweep(later)["abandoned"] == 0
    assert UploadSession.objects.get().state == FileState.STAGED


def test_a_stray_part_file_with_no_upload_is_removed_when_old(office):
    layout.staging_path("orphan").parent.mkdir(parents=True, exist_ok=True)
    old, fresh = layout.staging_path("orphan-old"), layout.staging_path("orphan-new")
    old.write_bytes(b"x")
    fresh.write_bytes(b"x")
    long_ago = now().timestamp() - 3 * 86400
    os.utime(old, (long_ago, long_ago))
    assert services.staging_sweep()["stray_parts"] == 1
    assert not old.exists() and fresh.exists()


def test_the_job_is_registered_for_04_45_daily():
    import ots.files.jobs  # noqa: F401

    job = registered_jobs()["files.staging_sweep"]
    assert job.schedule == "45 4 * * *"
