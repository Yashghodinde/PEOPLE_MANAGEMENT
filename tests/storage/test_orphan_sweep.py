"""M05.6: the orphan sweep adopts our own half-finished uploads and quarantines strangers. It never deletes."""

import datetime

import pytest
from freezegun import freeze_time
from ots.audit.models import AuditLog
from ots.core import alerts
from ots.core.jobs import registered_jobs
from ots.storage import classify as c
from ots.storage import jobs as storage_jobs  # noqa: F401  (registers the jobs)
from ots.storage import queue, sweep
from ots.storage.errors import GraphError
from ots.storage.gate import QueueGate
from ots.storage.handlers import clear_handlers, register_completion_handler
from ots.storage.models import StorageOperation as Op
from ots.storage.provider import Library

from tests.storage.queue_helpers import make_op

pytestmark = pytest.mark.django_db
K, S = Op.Kind, Op.State
LATER = "2030-01-01 06:00:00"  # the sweep runs at 02:30: well after anything was written


@pytest.fixture(autouse=True)
def _clean():
    clear_handlers()
    yield
    clear_handlers()


@pytest.fixture
def gate():
    return QueueGate(rng=lambda: 1.0)


def sweep_at_night(fake, gate):
    with freeze_time(LATER):
        return sweep.run(provider=fake, gate=gate)


def test_a_stranger_is_moved_to_quarantine_not_deleted(fake, gate):
    stray = fake.plant(Library.BUSINESS, "Files/2026/11/uploaded-by-hand.docx", b"someone dragged this in")
    stats = sweep_at_night(fake, gate)
    assert stats["quarantined"] == 1
    assert fake.find_by_path(Library.BUSINESS, "Files/2026/11/uploaded-by-hand.docx") is None
    quarantined = [p for p in fake.paths(Library.BUSINESS) if p.startswith("_quarantine/")]
    assert len(quarantined) == 1 and "uploaded-by-hand.docx" in quarantined[0]
    assert (
        b"".join(fake.open_read(stray)) == b"someone dragged this in"
    )  # same item, same bytes: moved, not copied
    assert alerts.open_alerts().get(key=c.ALERT_ORPHAN).severity == "WARNING"
    row = AuditLog.objects.get(action="storage.orphan_quarantined")
    assert row.payload["path"] == "Files/2026/11/uploaded-by-hand.docx" and row.actor_id is None


def test_a_second_sweep_leaves_quarantined_files_alone(fake, gate):
    fake.plant(Library.BUSINESS, "Files/x", b"stray")
    sweep_at_night(fake, gate)
    assert not sweep_at_night(fake, gate)
    assert len([p for p in fake.paths(Library.BUSINESS) if p.startswith("_quarantine/")]) == 1


def test_files_we_archived_are_not_orphans(fake, gate, tmp_path):
    make_op(tmp_path, b"ours")
    queue.process_one(provider=fake, gate=gate)
    assert not sweep_at_night(fake, gate)
    assert fake.find_by_path(Library.BUSINESS, "Files/2026/11/F-1") is not None


def test_a_file_written_in_the_last_fifteen_minutes_is_left_alone(fake, gate):
    with freeze_time("2030-01-01 05:50:00"):
        fake.plant(Library.BUSINESS, "Files/fresh", b"just now")
    with freeze_time("2030-01-01 06:00:00"):  # 10 minutes later
        assert sweep.run(provider=fake, gate=gate)["too_recent"] == 1
    with freeze_time("2030-01-01 06:10:00"):  # 20 minutes later
        assert sweep.run(provider=fake, gate=gate)["quarantined"] == 1


def test_the_dr_library_is_not_swept(fake, gate):
    fake.plant(Library.DR, "PostgreSQL/WAL/1/segment", b"wal")
    assert not sweep_at_night(fake, gate)
    assert fake.find_by_path(Library.DR, "PostgreSQL/WAL/1/segment") is not None


def test_our_own_upload_that_never_got_recorded_is_adopted(fake, gate, tmp_path):
    done = []
    register_completion_handler(K.ARCHIVE_FILE, lambda op, ref: done.append(ref.item_id))
    op = make_op(tmp_path, b"uploaded but not recorded")
    fake.plant(Library.BUSINESS, op.remote_path, b"uploaded but not recorded")  # it reached SharePoint
    stats = sweep_at_night(fake, gate)
    op.refresh_from_db()
    assert stats["adopted"] == 1 and op.state == S.DONE and done == [op.item_id]
    assert (
        len(fake.paths(Library.BUSINESS)) == 1
        and not alerts.open_alerts().filter(key=c.ALERT_ORPHAN).exists()
    )


def test_a_repair_upload_that_was_not_recorded_is_adopted_too(fake, gate, tmp_path):
    op = make_op(tmp_path, b"repaired", kind=K.REPAIR_FILE)
    fake.plant(Library.BUSINESS, op.remote_path, b"repaired")
    assert sweep_at_night(fake, gate)["adopted"] == 1


def test_a_file_at_our_path_with_different_content_is_neither_adopted_nor_quarantined(fake, gate, tmp_path):
    op = make_op(tmp_path, b"what we meant to upload")
    fake.plant(Library.BUSINESS, op.remote_path, b"a different file")
    stats = sweep_at_night(fake, gate)
    op.refresh_from_db()
    assert stats["left_for_conflict"] == 1 and op.state == S.PENDING
    assert (
        fake.find_by_path(Library.BUSINESS, op.remote_path) is not None
    )  # the upload's own 409 handling decides


def test_an_operation_a_worker_is_running_right_now_is_not_interfered_with(fake, gate, tmp_path):
    op = make_op(tmp_path, b"in flight")
    Op.objects.filter(pk=op.pk).update(state=S.RUNNING)
    fake.plant(Library.BUSINESS, op.remote_path, b"in flight")
    assert sweep_at_night(fake, gate)["in_flight"] == 1
    assert Op.objects.get(pk=op.pk).state == S.RUNNING


def test_a_dead_lettered_conflict_does_not_protect_the_strangers_file(fake, gate, tmp_path):
    op = make_op(tmp_path, b"ours")
    Op.objects.filter(pk=op.pk).update(state=S.FAILED)
    fake.plant(Library.BUSINESS, op.remote_path, b"theirs")
    assert sweep_at_night(fake, gate)["quarantined"] == 1  # the name becomes free for a repair


def test_adopting_works_even_when_the_cache_copy_is_gone(fake, gate, tmp_path):
    op = make_op(tmp_path, b"evicted from cache")
    (tmp_path / "F-1.bin").unlink()
    fake.plant(Library.BUSINESS, op.remote_path, b"evicted from cache")
    assert (
        sweep_at_night(fake, gate)["adopted"] == 1
    )  # compared by downloading, not by the missing local file


def test_a_blocked_queue_skips_the_sweep(fake, gate):
    fake.plant(Library.BUSINESS, "Files/x", b"stray")
    with freeze_time(LATER):
        gate.failure(GraphError(403))
        assert sweep.run(provider=fake, gate=gate)["skipped"] == 1
    assert fake.find_by_path(Library.BUSINESS, "Files/x") is not None


def test_a_failing_listing_is_reported_and_nothing_is_moved(fake, gate):
    fake.plant(Library.BUSINESS, "Files/x", b"stray")
    fake.inject([502])
    with freeze_time(LATER), pytest.raises(GraphError):
        sweep.run(provider=fake, gate=gate)
    assert fake.find_by_path(Library.BUSINESS, "Files/x") is not None


def test_the_job_runs_at_half_past_two():
    assert registered_jobs()["storage.orphan_sweep"].schedule == "30 2 * * *"


def test_the_sweep_never_calls_a_delete(fake, gate):
    fake.plant(Library.BUSINESS, "Files/x", b"stray")
    sweep_at_night(fake, gate)
    assert "delete_dr" not in fake.calls and "remote_delete" not in fake.calls
    assert datetime.timedelta(minutes=15) == sweep.GRACE
