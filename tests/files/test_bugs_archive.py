"""M08.4 bug hunt: edges found by reading the archive, cache and download code with suspicion."""

import asyncio
import json
import threading
import time
import uuid

import pytest
from ots.core.models import OutboxEvent
from ots.files import cache, download
from ots.files.models import Blob, FileFetchRequest, FileVersion
from ots.storage.models import StorageOperation as Op

from tests.files.conftest import drain, login, upload_all

pytestmark = pytest.mark.django_db
DATA = bytes(range(256)) * 12


def manifest_codes(settings):
    out = []
    for f in settings.OTS_MANIFEST_DIR.glob("manifest-*.jsonl"):
        out += [json.loads(line)["file_code"] for line in f.read_text().splitlines()]
    return out


def test_a_version_whose_content_was_already_archived_is_still_listed_and_announced(
    office, remote, settings, django_capture_on_commit_callbacks
):
    """Same bytes uploaded again after the first copy reached SharePoint: the new version has its own file code,
    so it needs its own manifest line (the disaster-recovery list) and its own file.archived event."""
    c = login("emp")
    upload_all(c, DATA, uuid.uuid4(), office["task"], name="a.xlsx")
    drain(remote)
    with django_capture_on_commit_callbacks(execute=True):
        upload_all(c, DATA, uuid.uuid4(), office["task"], name="b.xlsx")
    codes = set(FileVersion.objects.values_list("file_code", flat=True))
    assert len(codes) == 2 and Op.objects.count() == 1  # no second upload to SharePoint
    assert set(manifest_codes(settings)) == codes
    assert OutboxEvent.objects.filter(type="file.archived").count() == 2


def test_the_stream_wrapper_survives_being_cancelled_while_a_chunk_is_being_read():
    """A browser closing the tab mid-chunk must not leave the source generator 'already executing'."""
    closed = threading.Event()

    def source():
        try:
            for i in range(10):
                time.sleep(0.3)  # a slow SharePoint read
                yield bytes([i]) * 10
        finally:
            closed.set()

    async def scenario():
        agen = download.aiter_blocking(source())
        first = asyncio.ensure_future(agen.__anext__())
        await asyncio.sleep(0.1)  # the thread is now inside next()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        await agen.aclose()

    asyncio.run(scenario())
    assert closed.wait(3), "the source generator was never closed"


def test_an_empty_remote_stream_does_not_crash_the_tee(office):
    blob = Blob(sha256="a" * 64, size_bytes=0)
    gen = download._tee(blob, b"", iter(()))
    assert list(gen) == [b""]


def test_an_upload_landing_while_eviction_runs_never_leaves_a_cached_flag_without_a_file(office, remote):
    """Upload publishes the bytes, eviction deletes them, upload then marks the blob cached: the flag must be true."""
    c = login("emp")
    upload_all(c, DATA, uuid.uuid4(), office["task"], name="a.xlsx")
    drain(remote)
    from ots.files import services

    real = services._publish_bytes

    def publish_then_evict(part, sha):
        dest = real(part, sha)
        cache.evict(0)  # eviction wins the race right after the bytes were seen
        return dest

    services._publish_bytes = publish_then_evict
    try:
        upload_all(c, DATA, uuid.uuid4(), office["task"], name="b.xlsx")
    finally:
        services._publish_bytes = real
    blob = Blob.objects.get()
    from pathlib import Path

    assert (not blob.cached) or Path(blob.local_path).exists(), "cached flag set for a file that is gone"


# --- the fetch job must not hammer SharePoint or starve other requests ----------------------------------------


def _archived_and_evicted(office, remote, name, data):
    upload_all(login("emp"), data, uuid.uuid4(), office["task"], name=name)
    drain(remote)
    cache.evict(0)


def _ask(who, version, remote, monkeypatch):
    from ots.storage.errors import TransportFailure

    real = remote.open_read

    def down(ref):
        raise TransportFailure("down")
        yield b""

    monkeypatch.setattr(remote, "open_read", down)
    assert login(who).get(f"/files/v/{version.pk}/download").status_code == 503
    monkeypatch.setattr(remote, "open_read", real)


def test_a_corrupt_copy_is_retried_with_growing_pauses_not_every_minute(office, remote, monkeypatch):
    from ots.files import fetch

    _archived_and_evicted(office, remote, "a.xlsx", DATA)
    v = FileVersion.objects.get()
    _ask("sub", v, remote, monkeypatch)
    remote._items[Blob.objects.get().item_id].file.write_bytes(b"\x00" * len(DATA))
    first = fetch.run()
    assert first["waiting"] == 1
    req = FileFetchRequest.objects.get()
    assert req.attempts == 1 and req.next_attempt_at > req.created_at
    reads = []
    real = remote.open_read
    monkeypatch.setattr(remote, "open_read", lambda ref: reads.append(1) or real(ref))
    for _ in range(5):  # the next minutes: nothing is downloaded again
        assert fetch.run()["waiting"] == 0
    assert reads == []


def test_a_corrupt_file_does_not_block_the_files_behind_it(office, remote, monkeypatch):
    from ots.files import fetch

    versions = []
    for i in range(4):  # four different files wanted at once; the first three are corrupt in SharePoint
        _archived_and_evicted(office, remote, f"f{i}.xlsx", DATA + bytes([i]))
    versions = list(FileVersion.objects.order_by("uploaded_at"))
    for v in versions:
        _ask("sub", v, remote, monkeypatch)
    for v in versions[:3]:
        remote._items[v.blob.item_id].file.write_bytes(b"\x00" * v.size_bytes)
    fetch.run()  # tries the three oldest, all corrupt, and backs them off
    assert fetch.run()["fetched"] == 1  # the fourth is reached on the very next run
    assert FileFetchRequest.objects.filter(state="DONE").count() == 1


def test_an_unreachable_sharepoint_is_retried_every_minute_not_backed_off(office, remote, monkeypatch):
    from ots.files import fetch
    from ots.storage.errors import TransportFailure

    _archived_and_evicted(office, remote, "a.xlsx", DATA)
    _ask("sub", FileVersion.objects.get(), remote, monkeypatch)

    def down(ref):
        raise TransportFailure("down")
        yield b""

    monkeypatch.setattr(remote, "open_read", down)
    for _ in range(3):
        assert fetch.run()["waiting"] == 1
    assert FileFetchRequest.objects.get().attempts == 0  # being offline is not the file's fault
    monkeypatch.undo()
    assert fetch.run()["fetched"] == 1


def test_the_backoff_is_two_four_eight_minutes_and_stops_at_an_hour():
    from ots.files.fetch import backoff

    assert [backoff(n).total_seconds() / 60 for n in (1, 2, 3, 4, 5, 6, 7, 20)] == [
        2,
        4,
        8,
        16,
        32,
        60,
        60,
        60,
    ]
