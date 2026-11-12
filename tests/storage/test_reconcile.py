"""M05.5: nightly reconciliation finds deleted, renamed and changed items and repairs what it can."""

import pytest
from freezegun import freeze_time
from ots.core import alerts
from ots.core.jobs import registered_jobs
from ots.storage import classify as c
from ots.storage import jobs as storage_jobs  # noqa: F401  (registers the jobs)
from ots.storage import queue, reconcile
from ots.storage.errors import GraphError
from ots.storage.gate import QueueGate
from ots.storage.handlers import ReconcileHooks, clear_handlers, register_reconcile_hooks
from ots.storage.models import StorageCursor
from ots.storage.models import StorageOperation as Op
from ots.storage.provider import Library, RemoteRef

from tests.storage.queue_helpers import make_op

pytestmark = pytest.mark.django_db
K, S, R = Op.Kind, Op.State, Op.RemoteState


@pytest.fixture(autouse=True)
def _clean_hooks():
    clear_handlers()
    yield
    clear_handlers()


@pytest.fixture
def gate():
    return QueueGate(rng=lambda: 1.0)


def archive(fake, gate, tmp_path, data=b"precious", name="F-1", **kw) -> Op:
    op = make_op(tmp_path, data, name=name, **kw)
    assert queue.process_one(provider=fake, gate=gate) == "done"
    op.refresh_from_db()
    return op


def ref_of(op: Op) -> RemoteRef:
    return RemoteRef(Library(op.target_library), op.drive_id, op.item_id)


def run(fake, gate):
    return reconcile.run(provider=fake, gate=gate)


def keys():
    return {a.key for a in alerts.open_alerts()}


def test_a_healthy_archive_is_left_alone_and_the_cursors_are_saved(fake, gate, tmp_path):
    op = archive(fake, gate, tmp_path)
    assert op.remote_hash and op.remote_state == R.OK  # the verified hash was recorded at upload
    stats = run(fake, gate)
    assert not stats and not ({c.ALERT_MISSING, c.ALERT_DRIFTED, c.ALERT_CONTENT_CHANGED} & keys())
    assert set(StorageCursor.objects.values_list("library", flat=True)) == {"BUSINESS", "DR"}
    assert not run(fake, gate)  # a second night with no changes: nothing happens


def test_a_deleted_file_is_marked_missing_alerted_and_repaired_from_the_cache(fake, gate, tmp_path):
    op = archive(fake, gate, tmp_path, b"keep me")
    run(fake, gate)
    fake.remote_delete(ref_of(op))
    assert run(fake, gate)["missing"] == 1
    op.refresh_from_db()
    assert op.remote_state == R.MISSING
    assert alerts.open_alerts().get(key=c.ALERT_MISSING).severity == "CRITICAL"
    repair = Op.objects.get(kind=K.REPAIR_FILE)
    assert (repair.remote_path, repair.local_path, repair.sha256) == (
        op.remote_path,
        op.local_path,
        op.sha256,
    )
    assert queue.process_one(provider=fake, gate=gate) == "done"  # the repair runs like any upload
    assert b"".join(fake.open_read(fake.find_by_path(Library.BUSINESS, op.remote_path).ref)) == b"keep me"


def test_a_missing_file_with_no_cached_copy_is_alerted_but_cannot_be_repaired(fake, gate, tmp_path):
    op = archive(fake, gate, tmp_path)
    run(fake, gate)
    (tmp_path / "F-1.bin").unlink()  # the cache no longer has it
    fake.remote_delete(ref_of(op))
    run(fake, gate)
    assert c.ALERT_MISSING in keys() and not Op.objects.filter(kind=K.REPAIR_FILE).exists()


def test_a_taken_name_gets_a_restored_suffix_with_the_date(fake, gate, tmp_path):
    op = archive(fake, gate, tmp_path, b"original")
    run(fake, gate)
    fake.remote_delete(ref_of(op))
    fake.plant(Library.BUSINESS, op.remote_path, b"someone put another file here")
    with freeze_time("2030-01-01 06:00:00"):
        run(fake, gate)
    repair = Op.objects.get(kind=K.REPAIR_FILE)
    assert repair.remote_path == f"{op.remote_path}.restored-20300101"


def test_a_rename_is_harmless_the_path_is_updated_and_it_is_only_an_info(fake, gate, tmp_path):
    op = archive(fake, gate, tmp_path)
    run(fake, gate)
    fake.remote_rename(ref_of(op), "Files/2026/11/moved-by-hand")
    assert run(fake, gate)["moved"] == 1
    op.refresh_from_db()
    assert op.remote_path == "Files/2026/11/moved-by-hand" and op.remote_state == R.OK
    assert alerts.open_alerts().get(key=c.ALERT_DRIFTED).severity == "INFO"
    assert c.ALERT_MISSING not in keys() and not Op.objects.filter(kind=K.REPAIR_FILE).exists()


def test_changed_content_is_drifted_with_a_critical_alert_and_is_not_overwritten(fake, gate, tmp_path):
    op = archive(fake, gate, tmp_path, b"as archived")
    run(fake, gate)
    fake.remote_modify(ref_of(op), b"edited in SharePoint")
    assert run(fake, gate)["content_changed"] == 1
    op.refresh_from_db()
    assert op.remote_state == R.DRIFTED
    assert alerts.open_alerts().get(key=c.ALERT_CONTENT_CHANGED).severity == "CRITICAL"
    assert not Op.objects.filter(kind=K.REPAIR_FILE).exists()  # a person decides; nothing is replaced


def test_running_again_does_not_repeat_the_work(fake, gate, tmp_path):
    op = archive(fake, gate, tmp_path)
    run(fake, gate)
    fake.remote_delete(ref_of(op))
    run(fake, gate)
    count = alerts.open_alerts().get(key=c.ALERT_MISSING).count
    StorageCursor.objects.all().delete()  # even a full re-listing must not act twice
    assert not run(fake, gate)
    assert Op.objects.filter(kind=K.REPAIR_FILE).count() == 1
    assert alerts.open_alerts().get(key=c.ALERT_MISSING).count == count


def test_an_expired_cursor_falls_back_to_comparing_the_full_listing(fake, gate, tmp_path):
    op = archive(fake, gate, tmp_path)
    run(fake, gate)
    fake.remote_delete(ref_of(op))
    fake.expire_cursors()  # Graph will answer 410 for the saved cursor
    assert run(fake, gate)["missing"] == 1
    assert Op.objects.get(pk=op.pk).remote_state == R.MISSING


def test_the_first_run_notices_a_file_that_was_already_gone(fake, gate, tmp_path):
    op = archive(fake, gate, tmp_path)
    fake.remote_delete(ref_of(op))  # deleted before any cursor exists
    assert run(fake, gate)["missing"] == 1


def test_items_we_do_not_know_are_not_touched(fake, gate, tmp_path):
    fake.plant(Library.BUSINESS, "Files/2026/11/stray", b"orphan")  # the orphan sweep (M05.6) handles these
    assert not run(fake, gate)
    assert fake.find_by_path(Library.BUSINESS, "Files/2026/11/stray") is not None


def test_a_lost_backup_object_alerts_but_is_never_repaired_from_a_cache(fake, gate, tmp_path):
    wal = archive(
        fake, gate, tmp_path, b"wal segment", name="w", kind=K.SHIP_WAL, library=Library.DR, remote="WAL/1"
    )
    run(fake, gate)
    fake.remote_delete(ref_of(wal))
    run(fake, gate)
    assert Op.objects.get(pk=wal.pk).remote_state == R.MISSING and c.ALERT_MISSING in keys()
    assert not Op.objects.filter(kind=K.REPAIR_FILE).exists()


def test_owning_modules_are_told_through_hooks(fake, gate, tmp_path):
    seen = []
    register_reconcile_hooks(
        K.ARCHIVE_FILE,
        ReconcileHooks(
            on_missing=lambda op, meta: seen.append(("missing", op.pk)),
            on_moved=lambda op, meta: seen.append(("moved", meta.path)),
            on_drifted=lambda op, meta: seen.append(("drifted", op.pk)),
        ),
    )
    a = archive(fake, gate, tmp_path, b"aaa", name="a")
    b = archive(fake, gate, tmp_path, b"bbb", name="b")
    d = archive(fake, gate, tmp_path, b"ddd", name="d")
    run(fake, gate)
    fake.remote_delete(ref_of(a))
    fake.remote_rename(ref_of(b), "Files/2026/11/b-renamed")
    fake.remote_modify(ref_of(d), b"changed")
    run(fake, gate)
    assert sorted(seen) == sorted(
        [("missing", a.pk), ("moved", "Files/2026/11/b-renamed"), ("drifted", d.pk)]
    )


def test_a_blocked_queue_skips_the_run_and_keeps_the_cursors(fake, gate, tmp_path):
    archive(fake, gate, tmp_path)
    with freeze_time("2030-01-01 06:00:00"):
        gate.failure(GraphError(403))
        assert run(fake, gate)["skipped"] == 1
    assert not StorageCursor.objects.exists()


def test_a_failure_halfway_leaves_the_cursor_where_it_was(fake, gate, tmp_path):
    archive(fake, gate, tmp_path)
    fake.inject([None, 502])  # BUSINESS listing works, DR listing fails
    with pytest.raises(GraphError):
        run(fake, gate)
    assert set(StorageCursor.objects.values_list("library", flat=True)) == {
        "BUSINESS"
    }  # DR will retry in full


def test_the_job_is_registered_for_two_in_the_morning():
    jobs = registered_jobs()
    assert (
        jobs["storage.delta_reconcile"].schedule == "0 2 * * *" and not jobs["storage.delta_reconcile"].heavy
    )
    assert jobs["storage.process_queue"].schedule == "continuous"


def test_a_change_that_graph_reports_twice_is_acted_on_once(fake, gate, tmp_path):
    op = archive(fake, gate, tmp_path)
    ref = ref_of(op)
    fake.remote_delete(ref)
    gone = fake.changes(Library.BUSINESS, None)  # nothing listed: build the deletion notice by hand
    from ots.core.clock import now
    from ots.storage.provider import RemoteMeta

    notice = RemoteMeta(ref, "", "", 0, None, True, now())
    first = reconcile._apply(fake, Library.BUSINESS, [notice], full=False)
    second = reconcile._apply(fake, Library.BUSINESS, [notice], full=False)  # the same notice arrives again
    assert (first["missing"], second["missing"], gone[0]) == (1, 0, [])
    assert Op.objects.filter(kind=K.REPAIR_FILE).count() == 1
    assert alerts.open_alerts().get(key=c.ALERT_MISSING).count == 1
