"""F-FL-3: disk nearly full. An upload that would leave less than 10% of the disk free is refused with 507
"Server storage full", after the cache was first asked to evict. Enough room after eviction lets it through."""

import collections
import uuid

import pytest
from ots.files import cache, layout
from ots.files.models import UploadSession

from tests.files.conftest import login, start

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("dirs")]
DATA = b"x" * 1000
Usage = collections.namedtuple("Usage", "total used free")


def disk(monkeypatch, free_mb: float, total_mb: float = 1000.0):
    state = {"free": int(free_mb * 1e6)}
    monkeypatch.setattr(layout.shutil, "disk_usage", lambda p: Usage(int(total_mb * 1e6), 0, state["free"]))
    return state


def test_a_nearly_full_disk_refuses_the_upload_after_trying_to_evict(office, settings, monkeypatch):
    settings.OTS_DISK_FREE_MIN_PERCENT = 10
    disk(monkeypatch, free_mb=100)  # 10% of 1000 MB is exactly the floor: nothing to spare
    asked = []
    monkeypatch.setattr(cache, "evict_on_demand", lambda need=0: asked.append(need) or 0)
    r = start(login("emp"), DATA, uuid.uuid4(), office["task"])
    assert r.status_code == 507 and "storage full" in r.json()["message"].lower()
    assert len(asked) == 1 and asked[0] > 0  # eviction was tried first, told how much is missing
    assert UploadSession.objects.count() == 0


def test_eviction_that_frees_enough_lets_the_upload_through(office, settings, monkeypatch):
    settings.OTS_DISK_FREE_MIN_PERCENT = 10
    state = disk(monkeypatch, free_mb=100)

    def evict(need=0):
        state["free"] += int(300e6)
        return int(300e6)

    monkeypatch.setattr(cache, "evict_on_demand", evict)
    assert start(login("emp"), DATA, uuid.uuid4(), office["task"]).status_code == 201


def test_the_reservation_is_size_times_one_point_one(office, settings, monkeypatch):
    settings.OTS_DISK_FREE_MIN_PERCENT = 10
    disk(monkeypatch, free_mb=205)  # floor 100 MB; a 100 MB file reserves 110 MB: 205-110 = 95 < 100
    monkeypatch.setattr(cache, "evict_on_demand", lambda need=0: 0)
    big = {"size": 100 * 1000 * 1000, "sha256": "a" * 64}
    c = login("emp")
    body = {
        "idempotency_key": str(uuid.uuid4()),
        "original_filename": "big.bin",
        "task_id": str(office["task"].pk),
        **big,
    }
    assert (
        c.post(
            "/files/uploads", body, content_type="application/json", HTTP_ACCEPT="application/json"
        ).status_code
        == 507
    )
    disk(monkeypatch, free_mb=215)  # 215-110 = 105 >= 100
    body["idempotency_key"] = str(uuid.uuid4())
    assert (
        c.post(
            "/files/uploads", body, content_type="application/json", HTTP_ACCEPT="application/json"
        ).status_code
        == 201
    )
