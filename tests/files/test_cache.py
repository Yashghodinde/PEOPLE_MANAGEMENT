"""M08.4: cache eviction (section 4) and filling the cache from SharePoint."""

import datetime
import hashlib
import os
import time
import uuid

import pytest
from ots.core.clock import now
from ots.core.jobs import registered_jobs
from ots.files import cache, layout
from ots.files.models import Blob, FileState
from ots.storage import queue

from tests.files.conftest import drain, login, upload_all
from tests.files.factories import make_file

pytestmark = pytest.mark.django_db
HOUR = datetime.timedelta(hours=1)


def cached_blob(data: bytes, state=FileState.ARCHIVED, age_hours: float = 0) -> Blob:
    """A blob with a real file in the cache, last used `age_hours` ago."""
    sha = hashlib.sha256(data).hexdigest()
    path = layout.blob_path(sha)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return Blob.objects.create(
        sha256=sha, size_bytes=len(data), local_path=str(path), cached=True, state=state,
        last_accessed_at=now() - datetime.timedelta(hours=age_hours),
    )  # fmt: skip


def test_only_archived_blobs_are_ever_evicted():
    kept = {}
    for state in FileState.values:
        kept[state] = cached_blob(state.encode() * 50, state=state)
    freed = cache.evict(0)
    for state, blob in kept.items():
        blob.refresh_from_db()
        if state == FileState.ARCHIVED:
            assert not blob.cached and blob.local_path is None and not layout.blob_path(blob.sha256).exists()
        else:
            assert blob.cached and layout.blob_path(blob.sha256).exists(), state
    assert freed == kept[FileState.ARCHIVED].size_bytes


def test_the_least_recently_used_go_first_and_it_stops_at_the_target():
    old, mid, new = (cached_blob(bytes([i]) * 100, age_hours=h) for i, h in ((1, 30), (2, 20), (3, 10)))
    assert cache.evict(150) == 200  # 300 held, target 150: the two oldest go (100 would not be enough)
    for blob, gone in ((old, True), (mid, True), (new, False)):
        blob.refresh_from_db()
        assert blob.cached is not gone


def test_a_blob_a_pending_storage_operation_still_needs_is_kept():
    pinned, free = cached_blob(b"p" * 100, age_hours=50), cached_blob(b"f" * 100, age_hours=40)
    queue.enqueue("REPAIR_FILE", __import__("ots.storage.provider", fromlist=["Library"]).Library.BUSINESS,
                  pinned.local_path, "x/y", pinned.sha256, 100)  # fmt: skip
    cache.evict(0)
    pinned.refresh_from_db(), free.refresh_from_db()
    assert pinned.cached and not free.cached


def test_nothing_happens_below_ninety_percent_and_it_drops_to_eighty_above(settings):
    settings.OTS_CACHE_CAP_GB = 1000 / 1024**3  # a cap of 1000 bytes
    blobs = [cached_blob(bytes([i]) * 100, age_hours=20 - i) for i in range(1, 9)]  # 800 bytes
    assert cache.run() == 0 and all(Blob.objects.get(pk=b.pk).cached for b in blobs)
    cached_blob(b"z" * 100, age_hours=0)  # 900: exactly 90%, still not above
    assert cache.run() == 0
    cached_blob(b"y" * 100, age_hours=0)  # 1000 bytes: above 90%
    assert cache.run() == 200  # down to 800 (80%)
    assert cache.cached_bytes() == 800
    assert not Blob.objects.get(pk=blobs[0].pk).cached and not Blob.objects.get(pk=blobs[1].pk).cached


def test_the_daily_job_is_registered_for_02_45():
    import ots.files.jobs  # noqa: F401

    assert registered_jobs()["files.cache_evict"].schedule == "45 2 * * *"


def test_an_upload_that_pushes_the_cache_over_the_line_trims_it(
    office, remote, settings, django_capture_on_commit_callbacks
):
    settings.OTS_CACHE_CAP_GB = 3 * 3072 / 1024**3  # room for three 3 KB files
    c = login("emp")
    for i in range(3):
        upload_all(c, bytes([i]) * 3072, uuid.uuid4(), office["task"], name=f"f{i}.xlsx")
    drain(remote)  # all archived
    with django_capture_on_commit_callbacks(execute=True):
        upload_all(c, b"\x09" * 3072, uuid.uuid4(), office["task"], name="f9.xlsx")
    assert cache.cached_bytes() <= 0.8 * cache.cap_bytes() + 3072
    assert (
        Blob.objects.filter(state=FileState.STAGED, cached=True).count() == 1
    )  # the new, unarchived one stays


def test_a_nearly_full_disk_evicts_archived_copies_before_refusing(office, remote, settings, monkeypatch):
    """F-FL-3 with the real cache: eviction frees the disk, and the upload then goes through."""
    c = login("emp")
    upload_all(c, b"\x01" * 3072, uuid.uuid4(), office["task"], name="big.xlsx")
    drain(remote)
    settings.OTS_DISK_FREE_MIN_PERCENT = 10  # only now: the real disk is not what this test is about
    usage = type("U", (), {})

    def disk(_path):
        u = usage()
        u.total = 100_000
        u.free = 10_000 + (0 if Blob.objects.filter(cached=True).exists() else 5000)  # eviction frees 5000
        return u

    monkeypatch.setattr(layout.shutil, "disk_usage", disk)
    r = login("emp").post(
        "/files/uploads",
        {"idempotency_key": str(uuid.uuid4()), "original_filename": "n.xlsx", "size": 3000, "sha256": "a" * 64,
         "task_id": str(office["task"].pk)},
        content_type="application/json", HTTP_ACCEPT="application/json",
    )  # fmt: skip
    assert r.status_code == 201, r.content
    assert not Blob.objects.get().cached  # the archived copy was evicted to make room


# --- filling the cache ---------------------------------------------------------------------------------------


def archived_remote(office, remote, data=b"\x07" * 4096):
    upload_all(login("emp"), data, uuid.uuid4(), office["task"], name="r.xlsx")
    drain(remote)
    blob = Blob.objects.get()
    cache.evict(0)
    blob.refresh_from_db()
    assert not blob.cached
    return blob


def test_fill_brings_the_file_back_verified(office, remote):
    blob = archived_remote(office, remote)
    assert cache.fill(blob, remote) == cache.OK
    blob.refresh_from_db()
    assert blob.cached and layout.blob_path(blob.sha256).read_bytes() == b"\x07" * 4096
    assert not cache.fetch_part_path(blob.sha256).exists()


def test_fill_refuses_bytes_that_do_not_match_and_leaves_nothing(office, remote):
    blob = archived_remote(office, remote)
    item = remote._items[blob.item_id]  # corrupt the remote copy, keeping its size
    item.file.write_bytes(b"\x08" * 4096)
    assert cache.fill(blob, remote) == cache.CORRUPT
    blob.refresh_from_db()
    assert not blob.cached and not layout.blob_path(blob.sha256).exists()
    assert not cache.fetch_part_path(blob.sha256).exists()


def test_fill_gives_up_quietly_when_sharepoint_is_unreachable(office, remote):
    blob = archived_remote(office, remote)
    remote.inject([503])
    assert cache.fill(blob, remote) == cache.UNREACHABLE
    assert not Blob.objects.get().cached and not cache.fetch_part_path(blob.sha256).exists()


def test_only_one_request_fills_the_cache_at_a_time_and_a_dead_one_is_reclaimed(settings):
    sha = "ab" * 32
    first = cache.claim_part(sha)
    assert first is not None and cache.claim_part(sha) is None  # a second request just streams
    old = time.time() - 2 * cache.STALE_PART_SECONDS
    os.utime(first, (old, old))
    assert cache.claim_part(sha) is not None  # the first process died long ago: take over


def test_a_cached_flag_with_no_file_is_not_trusted(office):
    f, v = make_file(office["emp"])
    Blob.objects.filter(pk=v.blob_id).update(
        cached=True, local_path="/nonexistent/file", state=FileState.ARCHIVED
    )
    from ots.files.download import _local_copy

    assert _local_copy(Blob.objects.get(pk=v.blob_id)) is None
    assert not Blob.objects.get(pk=v.blob_id).cached  # and the record now tells the truth
