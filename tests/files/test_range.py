"""IMP-10.2: HTTP Range for playback and seeking (206, 416), on cache hits and on misses, with one access row
per listen and a hash-verified cache fill."""

import uuid

import pytest
from ots.files import cache, download
from ots.files.models import Blob, FileAccessLog, FileVersion

from tests.files.conftest import body, drain, login, upload_all

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("dirs")]
AUDIO = bytes((i * 7) % 256 for i in range(10_000))


@pytest.fixture
def note(office, remote):
    c, task = login("emp"), office["task"]
    upload_all(c, AUDIO, uuid.uuid4(), task, name="voice.webm", relation="VOICE")
    drain(remote)
    version = FileVersion.objects.get()
    r = c.post(
        f"/app/tasks/{task.pk}/voice",
        {"version_id": str(version.pk), "seconds": 9},
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
    )
    assert r.status_code == 200
    return version


def play(who, version, rng=None, kind="preview"):
    extra = {"HTTP_RANGE": rng} if rng else {}
    return login(who).get(f"/files/v/{version.pk}/{kind}", **extra)


def test_without_a_range_the_whole_file_comes_with_accept_ranges(office, note):
    r = play("sub", note)
    assert r.status_code == 200 and body(r) == AUDIO
    assert r["Accept-Ranges"] == "bytes" and r["Content-Length"] == str(len(AUDIO))
    assert r["Content-Type"] == "audio/webm"


def test_a_middle_range_comes_back_as_206_with_exactly_those_bytes(office, note):
    r = play("sub", note, "bytes=2000-2999")
    assert r.status_code == 206
    assert body(r) == AUDIO[2000:3000]
    assert r["Content-Range"] == f"bytes 2000-2999/{len(AUDIO)}" and r["Content-Length"] == "1000"


@pytest.mark.parametrize(
    ("header", "first", "last"),
    [
        ("bytes=0-0", 0, 0), ("bytes=9999-", 9999, 9999), ("bytes=-500", 9500, 9999),
        ("bytes=9000-99999", 9000, 9999), ("bytes=0-", 0, 9999), ("bytes=-99999", 0, 9999),
    ],
)  # fmt: skip
def test_range_shapes(office, note, header, first, last):
    r = play("emp", note, header)
    assert r.status_code == 206 and body(r) == AUDIO[first : last + 1]
    assert r["Content-Range"] == f"bytes {first}-{last}/{len(AUDIO)}"


@pytest.mark.parametrize("header", ["bytes=10000-", "bytes=20000-30000", "bytes=-0", "bytes=5000-4000"])
def test_a_range_that_cannot_be_satisfied_is_416(office, note, header):
    r = play("sub", note, header)
    assert r.status_code == 416 and r["Content-Range"] == f"bytes */{len(AUDIO)}"


@pytest.mark.parametrize("header", ["bytes=0-5,10-20", "items=0-5", "bytes=abc", "bytes=-", ""])
def test_a_range_we_do_not_understand_is_ignored_and_the_whole_file_is_sent(office, note, header):
    r = play("sub", note, header)
    assert r.status_code == 200 and body(r) == AUDIO


def test_only_range_requests_that_start_at_byte_zero_are_logged_as_opens(office, note):
    for rng in ("bytes=0-1023", "bytes=1024-4095", "bytes=4096-", "bytes=0-"):
        assert play("sub", note, rng).status_code == 206
    rows = FileAccessLog.objects.filter(version=note)
    assert rows.count() == 2  # the two requests that began at byte 0; seeks and later pieces log nothing
    assert play("sub", note).status_code == 200  # a plain play is a new open
    assert FileAccessLog.objects.filter(version=note).count() == 3


def test_a_seek_alone_never_creates_an_access_row(office, note):
    play("sub", note, "bytes=5000-5999")
    assert not FileAccessLog.objects.exists()


def test_voice_is_private_cached_for_an_hour_and_other_files_stay_no_store(office, remote, note):
    r = play("sub", note)
    assert r["Cache-Control"] == "private, max-age=3600"
    upload_all(login("emp"), AUDIO + b"x", uuid.uuid4(), office["task"], name="2026-11-17_Sales-Report.xlsx")
    other = FileVersion.objects.exclude(pk=note.pk).get()
    assert "no-store" in login("sub").get(f"/files/v/{other.pk}/download")["Cache-Control"]


def test_an_evicted_note_is_filled_into_the_cache_then_the_range_is_served(office, remote, note):
    cache.evict(0)
    assert not Blob.objects.get().cached
    r = play("sub", note, "bytes=100-199")
    assert r.status_code == 206 and body(r) == AUDIO[100:200]
    assert Blob.objects.get().cached  # filled and hash-verified
    assert not cache.fetch_part_path(Blob.objects.get().sha256).exists()
    again = play("sub", note, "bytes=0-9")
    assert body(again) == AUDIO[:10]


def test_a_cache_fill_with_the_wrong_bytes_is_never_trusted(office, remote, note):
    cache.evict(0)
    blob = Blob.objects.get()
    remote._items[blob.item_id].file.write_bytes(b"\x00" * len(AUDIO))  # SharePoint returns other bytes
    r = play("sub", note, "bytes=0-99")
    assert r.status_code in (200, 206)
    assert not Blob.objects.get().cached  # the bad copy did not enter the cache
    assert not cache.fetch_part_path(blob.sha256).exists()


def test_a_large_uncached_file_ignores_the_range_and_streams_all_of_it(office, remote, monkeypatch):
    c = login("emp")
    upload_all(c, AUDIO, uuid.uuid4(), office["task"], name="2026-11-17_Big-Report.xlsx")
    drain(remote)
    cache.evict(0)
    monkeypatch.setattr(download, "RANGE_FILL_MAX", 100)
    v = FileVersion.objects.get()
    r = login("sub").get(f"/files/v/{v.pk}/download", HTTP_RANGE="bytes=10-19")
    assert r.status_code == 200 and body(r) == AUDIO  # not filled first, so the whole file is sent


def test_parse_range_unit():
    p = download.parse_range
    assert (
        p("bytes=0-9", 100) == (0, 9) and p("bytes=90-", 100) == (90, 99) and p("bytes=-10", 100) == (90, 99)
    )
    assert p("bytes=100-", 100) == "bad" and p("bytes=-5", 0) == "bad" and p("garbage", 100) is None
