"""M05.8: the monthly inventory: comparison, size mismatch, silent corruption found by the byte spot-check."""

import pytest
from ots.core import alerts
from ots.core.jobs import registered_jobs
from ots.storage import classify as c
from ots.storage import inventory, queue
from ots.storage import jobs as storage_jobs  # noqa: F401  (registers the jobs)
from ots.storage.errors import GraphError
from ots.storage.gate import QueueGate
from ots.storage.models import StorageOperation as Op
from ots.storage.provider import Library, RemoteRef

from tests.storage.queue_helpers import make_op

pytestmark = pytest.mark.django_db
R = Op.RemoteState


@pytest.fixture
def gate():
    return QueueGate(rng=lambda: 1.0)


def archive(fake, gate, tmp_path, data, name):
    op = make_op(tmp_path, data, name=name)
    assert queue.process_one(provider=fake, gate=gate) == "done"
    op.refresh_from_db()
    return op


def ref_of(op):
    return RemoteRef(Library(op.target_library), op.drive_id, op.item_id)


def inv(fake, gate, rate=0.0, rng=lambda: 0.0):
    return inventory.run(provider=fake, gate=gate, sample_rate=rate, rng=rng)


def test_a_healthy_archive_passes_with_every_file_spot_checked(fake, gate, tmp_path):
    for n in range(3):
        archive(fake, gate, tmp_path, f"file {n}".encode(), f"f{n}")
    stats = inv(fake, gate, rate=1.0)
    assert (stats["listed"], stats["spot_checked"], stats["spot_failed"], stats["missing"]) == (3, 3, 0, 0)
    assert not alerts.open_alerts().filter(key__in=[c.ALERT_MISSING, c.ALERT_CONTENT_CHANGED]).exists()


def test_an_injected_size_mismatch_is_detected(fake, gate, tmp_path):
    op = archive(fake, gate, tmp_path, b"original size", "f")
    fake.remote_modify(ref_of(op), b"much longer content than we archived")
    stats = inv(fake, gate)
    op.refresh_from_db()
    assert stats["content_changed"] == 1 and op.remote_state == R.DRIFTED
    assert alerts.open_alerts().get(key=c.ALERT_CONTENT_CHANGED).severity == "CRITICAL"


def test_same_size_different_bytes_is_caught_by_the_byte_spot_check(fake, gate, tmp_path):
    op = archive(fake, gate, tmp_path, b"AAAAAAAAAA", "f")
    fake._items[op.item_id].file.write_bytes(
        b"BBBBBBBBBB"
    )  # silent corruption: size and metadata hash unchanged
    assert inv(fake, gate)["spot_checked"] == 0  # without sampling, metadata alone cannot see it
    stats = inv(fake, gate, rate=1.0)
    op.refresh_from_db()
    assert stats["spot_failed"] == 1 and op.remote_state == R.DRIFTED
    assert alerts.open_alerts().get(key=c.ALERT_CONTENT_CHANGED).severity == "CRITICAL"


def test_sampling_follows_the_rate(fake, gate, tmp_path):
    for n in range(4):
        archive(fake, gate, tmp_path, f"x{n}".encode(), f"f{n}")
    assert inv(fake, gate, rate=0.02, rng=lambda: 0.5)["spot_checked"] == 0
    assert inv(fake, gate, rate=0.02, rng=lambda: 0.01)["spot_checked"] == 4
    assert inventory.SAMPLE_RATE == 0.02


def test_the_download_is_streamed_in_one_mebibyte_chunks(fake, gate, tmp_path):
    archive(fake, gate, tmp_path, b"z" * (2 * 1024 * 1024 + 9), "big")
    sizes = []
    original = fake.open_read

    def spy(ref):
        for chunk in original(ref):
            sizes.append(len(chunk))
            yield chunk

    fake.open_read = spy  # type: ignore[method-assign]
    inv(fake, gate, rate=1.0)
    assert sizes == [1024 * 1024, 1024 * 1024, 9]  # never the whole file at once


def test_a_deleted_file_is_found_from_the_full_listing(fake, gate, tmp_path):
    op = archive(fake, gate, tmp_path, b"gone soon", "f")
    fake.remote_delete(ref_of(op))
    assert inv(fake, gate)["missing"] == 1
    assert c.ALERT_MISSING in {a.key for a in alerts.open_alerts()}


def test_files_that_are_not_ours_are_counted_not_touched(fake, gate, tmp_path):
    fake.plant(Library.BUSINESS, "Files/2026/11/stray", b"stray")
    stats = inv(fake, gate)
    assert stats["unknown"] == 1 and fake.find_by_path(Library.BUSINESS, "Files/2026/11/stray") is not None


def test_a_file_that_vanishes_during_the_check_is_left_for_the_nightly_run(fake, gate, tmp_path):
    archive(fake, gate, tmp_path, b"here then gone", "f")

    def vanish(ref):
        raise GraphError(404, code="itemNotFound")
        yield b""  # pragma: no cover

    fake.open_read = vanish  # type: ignore[method-assign]
    assert inv(fake, gate, rate=1.0)["spot_failed"] == 0


def test_a_blocked_queue_skips_the_inventory(fake, gate, tmp_path):
    from freezegun import freeze_time

    with freeze_time("2030-01-01 06:00:00"):
        gate.failure(GraphError(403))
        assert inv(fake, gate)["skipped"] == 1


def test_the_job_is_heavy_and_monthly():
    job = registered_jobs()["storage.full_inventory"]
    assert job.schedule == "0 3 1 * *" and job.heavy
