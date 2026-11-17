"""M04.2: shipping WAL to SharePoint in order, recording it, and watching the recovery point (F-BK-3, F-BK-4)."""

import datetime
import os
from pathlib import Path

import pytest
from freezegun import freeze_time
from ots.backup import monitor, shipper, spool, wal
from ots.backup.models import BackupObject
from ots.core import alerts
from ots.storage import queue
from ots.storage.fake import LocalDirProvider
from ots.storage.gate import QueueGate
from ots.storage.models import StorageOperation as Op

pytestmark = pytest.mark.django_db
T0 = "2026-11-17 06:00:00"


@pytest.fixture(autouse=True)
def dirs(settings, tmp_path):
    settings.OTS_WAL_SPOOL_DIR = tmp_path / "spool"
    settings.OTS_WAL_LOCAL_DIR = tmp_path / "local"
    settings.OTS_WAL_SPOOL_DIR.mkdir()
    return tmp_path


@pytest.fixture(autouse=True)
def handlers():
    from ots.backup import handlers as h
    from ots.storage.handlers import clear_handlers

    clear_handlers()
    h.register()
    yield
    clear_handlers()


@pytest.fixture
def remote(tmp_path):
    return LocalDirProvider(tmp_path / "remote")


def name_of(n, timeline=1):
    return wal.Segment(timeline, 0, n).name


def put(n, settings, timeline=1, size=2048):
    """A segment the archive script would have left in the spool."""
    path = settings.OTS_WAL_SPOOL_DIR / f"{name_of(n, timeline)}.zst.age"
    path.write_bytes(os.urandom(size))
    return path


_capture = {}


@pytest.fixture(autouse=True)
def _on_commit(django_capture_on_commit_callbacks):
    """The test's own transaction never commits, so run the 'after commit' steps where a real commit would."""
    _capture["f"] = django_capture_on_commit_callbacks
    yield
    _capture.clear()


def utc(moment):
    """A frozen clock's value as an aware UTC time (a naive one would be read as local time)."""
    return moment.replace(tzinfo=datetime.UTC) if moment.tzinfo is None else moment


def pump(remote, gate=None, at=None):
    """Run the storage queue until it has nothing due; returns what it did."""
    gate = gate or QueueGate(rng=lambda: 1.0)
    out = []
    for _ in range(50):
        with _capture["f"](execute=True):
            r = queue.process_one(provider=remote, gate=gate)
        if r in ("idle", "waited"):
            break
        out.append(r)
    return out


# --- segment names ---------------------------------------------------------------------------------------------


def test_segment_names_give_their_position_and_end():
    s = wal.parse("000000010000000000000003")
    assert (s.timeline, s.log, s.seg, s.number) == (1, 0, 3, 3) and s.end_lsn == 4 * 16 * 1024 * 1024
    assert wal.parse("0000000100000001000000FF").number == 1 * 256 + 255
    assert wal.parse("000000010000000100000000").number == 256  # the next log follows 0xFF directly
    for bad in (
        "00000002.history",
        "000000010000000000000003.partial",
        "x",
        "",
        "000000010000000000000003.00000028.backup",
    ):
        assert wal.parse(bad) is None
    assert wal.gaps([1, 2, 3, 7, 8, 12]) == [(4, 6), (9, 11)] and wal.gaps([5]) == [] and wal.gaps([]) == []


# --- queueing and shipping ---------------------------------------------------------------------------------------


def test_new_spool_files_are_queued_once_in_name_order_with_the_right_remote_path(settings):
    for n in (5, 3, 4):
        put(n, settings)
    assert shipper.enqueue_wal() == 3
    assert shipper.enqueue_wal() == 0  # a second pass finds them already queued
    ops = list(Op.objects.filter(kind=Op.Kind.SHIP_WAL).order_by("created_at"))
    assert [o.ref_id for o in ops] == [name_of(3), name_of(4), name_of(5)]
    assert ops[0].remote_path == f"PostgreSQL/WAL/00000001/{name_of(3)}.zst.age"
    assert ops[0].target_library == "DR" and ops[0].priority == 0
    assert ops[0].sha256 == spool.sha256_of(Path(ops[0].local_path))


def test_half_written_files_are_never_queued(settings):
    (settings.OTS_WAL_SPOOL_DIR / f"{name_of(1)}.zst.age.tmp.123").write_bytes(b"partial")
    (settings.OTS_WAL_SPOOL_DIR / ".shipped").mkdir()
    assert shipper.enqueue_wal() == 0


def test_shipping_records_the_object_and_leaves_a_marker_instead_of_the_file(settings, remote):
    path = put(3, settings)
    with freeze_time(T0):
        shipper.enqueue_wal()
        pump(remote)
    obj = BackupObject.objects.get()
    assert (obj.type, obj.wal_segment, obj.wal_timeline, obj.wal_end_lsn) == (
        "WAL",
        name_of(3),
        1,
        wal.parse(name_of(3)).end_lsn,
    )
    assert obj.confirmed_at is not None and obj.pruned_at is None
    assert not path.exists() and (settings.OTS_WAL_SPOOL_DIR / ".shipped" / name_of(3)).stat().st_size == 0
    assert remote.find_by_path(
        Op.objects.get().target_library
        and __import__("ots.storage.provider", fromlist=["Library"]).Library.DR,
        obj.remote_path,
    )


def test_a_segment_never_shipped_again_after_its_marker_exists(settings, remote):
    put(3, settings)
    shipper.enqueue_wal()
    pump(remote)
    assert shipper.enqueue_wal() == 0 and Op.objects.count() == 1 and BackupObject.objects.count() == 1


def test_a_done_operation_whose_file_was_left_behind_is_tidied_not_requeued(settings, remote):
    path = put(3, settings)
    shipper.enqueue_wal()
    pump(remote)
    path.write_bytes(b"left behind by a crash after the commit")
    assert shipper.enqueue_wal() == 0
    assert not path.exists() and Op.objects.count() == 1


def test_a_failing_handler_keeps_the_file_and_the_operation_stays_open(settings, remote, monkeypatch):
    path = put(3, settings)
    shipper.enqueue_wal()

    def boom(**kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(shipper.BackupObject.objects, "update_or_create", boom)
    pump(remote)  # the queue logs the failure and retries later; it never claims "off-site"
    assert path.exists() and Op.objects.get().state != Op.State.DONE and not BackupObject.objects.exists()


def test_segments_ship_in_order_even_when_an_earlier_one_is_waiting_for_a_retry(settings, remote):
    put(3, settings)
    shipper.enqueue_wal()
    remote.inject([503])  # the first upload fails and goes to RETRY_WAIT
    pump(remote)
    put(4, settings)
    shipper.enqueue_wal()
    done_before = list(Op.objects.filter(state=Op.State.DONE).values_list("ref_id", flat=True))
    assert done_before == []  # 4 may not pass 3, even though 4 is due and 3 is not
    for op in Op.objects.filter(state=Op.State.RETRY_WAIT):
        Op.objects.filter(pk=op.pk).update(next_attempt_at=op.created_at)
    pump(remote)
    shipped = list(
        BackupObject.objects.order_by("confirmed_at", "wal_segment").values_list("wal_segment", flat=True)
    )
    assert shipped == [name_of(3), name_of(4)]


# --- the recovery point (F-BK-3) --------------------------------------------------------------------------------


def archived(settings, remote, n, when):
    put(n, settings)
    with freeze_time(when):
        shipper.enqueue_wal()
        pump(remote)
        BackupObject.objects.filter(wal_segment=name_of(n)).update(
            wal_end_time=utc(datetime.datetime.fromisoformat(when))
        )


def test_recovery_point_alert_levels(settings, remote):
    archived(settings, remote, 1, T0)
    key = "backup.rpo"
    with freeze_time("2026-11-17 06:09:30"):
        monitor.check()
        assert not alerts.open_alerts().filter(key=key).exists()
    with freeze_time("2026-11-17 06:10:30"):
        monitor.check()
        assert alerts.open_alerts().get(key=key).severity == "WARNING"
    with freeze_time("2026-11-17 06:15:30"):
        monitor.check()
        assert alerts.open_alerts().get(key=key).severity == "CRITICAL"
    archived(settings, remote, 2, "2026-11-17 06:16:00")
    with freeze_time("2026-11-17 06:16:30"):
        monitor.check()
        assert not alerts.open_alerts().filter(key=key).exists()


def test_internet_down_for_twenty_minutes_then_everything_ships_in_order(settings, remote):  # F-BK-3
    gate = QueueGate(rng=lambda: 1.0)
    archived(settings, remote, 1, T0)
    with freeze_time(T0) as clock:
        remote.inject([502] * 400)  # nothing reaches SharePoint
        for n in (2, 3, 4):
            clock.tick(datetime.timedelta(minutes=5))
            put(n, settings)
            shipper.enqueue_wal()
            pump(remote, gate)
            for op in Op.objects.filter(state=Op.State.RETRY_WAIT):
                Op.objects.filter(pk=op.pk).update(next_attempt_at=utc(clock()))
        clock.tick(datetime.timedelta(seconds=30))
        monitor.check()  # 15.5 minutes since the last confirmed segment
        assert alerts.open_alerts().get(key="backup.rpo").severity == "CRITICAL"
        clock.tick(datetime.timedelta(minutes=5))  # 20 minutes in total
        remote._failures.clear()  # the internet is back
        gate = QueueGate(rng=lambda: 1.0)
        for _ in range(8):
            for op in Op.objects.exclude(state=Op.State.DONE):
                Op.objects.filter(pk=op.pk).update(
                    next_attempt_at=utc(clock()),
                    state=Op.State.PENDING if op.state == Op.State.RETRY_WAIT else op.state,
                )
            pump(remote, gate)
        confirmed = list(
            BackupObject.objects.order_by("confirmed_at", "id").values_list("wal_segment", flat=True)
        )
        assert confirmed == [name_of(i) for i in (1, 2, 3, 4)]
        BackupObject.objects.filter(wal_segment__in=[name_of(i) for i in (2, 3, 4)]).update(
            wal_end_time=utc(clock())
        )
        monitor.check()
        assert not alerts.open_alerts().filter(key="backup.rpo").exists()


def test_before_the_first_segment_there_is_nothing_to_alarm_about():
    monitor.check()
    assert not alerts.open_alerts().filter(key="backup.rpo").exists()
    assert monitor.status().rpo_age is None


def test_a_first_segment_stuck_in_the_spool_does_alarm(settings):
    with freeze_time(T0):
        path = put(1, settings)
        os.utime(path, (datetime.datetime.fromisoformat(T0).timestamp(),) * 2)
        shipper.enqueue_wal()
    with freeze_time("2026-11-17 06:16:00"):
        monitor.check()
        assert alerts.open_alerts().get(key="backup.rpo").severity == "CRITICAL"


# --- the spool (F-BK-4) -----------------------------------------------------------------------------------------------


def test_a_spool_over_six_gigabytes_warns_and_over_seven_and_a_half_is_critical(settings):
    big = settings.OTS_WAL_SPOOL_DIR / f"{name_of(1)}.zst.age"
    with big.open("wb") as fh:
        fh.truncate(int(6.1 * 1024**3))  # sparse: no real disk is used
    monitor.check()
    assert alerts.open_alerts().get(key="backup.spool_size").severity == "WARNING"
    with big.open("r+b") as fh:
        fh.truncate(int(7.6 * 1024**3))
    monitor.check()
    assert alerts.open_alerts().get(key="backup.spool_size").severity == "CRITICAL"
    big.unlink()
    monitor.check()
    assert not alerts.open_alerts().filter(key="backup.spool_size").exists()


def test_markers_do_not_count_towards_the_spool_and_old_ones_are_pruned(settings):
    marker_dir = settings.OTS_WAL_SPOOL_DIR / ".shipped"
    marker_dir.mkdir()
    old, fresh = marker_dir / "old", marker_dir / "fresh"
    old.touch()
    fresh.touch()
    os.utime(old, (1, 1))
    assert spool.size_bytes() == 0
    assert spool.prune_markers(datetime.datetime.now(datetime.UTC)) == 1
    assert not old.exists() and fresh.exists()


# --- PostgreSQL's own archiver (F-BK-7) -------------------------------------------------------------------------


def test_the_archiver_alert_follows_the_database_status(monkeypatch):
    monkeypatch.setattr(monitor, "archiver_failing", lambda: True)
    monitor.check()
    assert alerts.open_alerts().get(key="backup.archiver_failing").severity == "CRITICAL"
    monkeypatch.setattr(monitor, "archiver_failing", lambda: False)
    monitor.check()
    assert not alerts.open_alerts().filter(key="backup.archiver_failing").exists()


def test_the_real_archiver_status_query_runs():
    assert monitor.archiver_failing() in (True, False)


def test_the_backup_jobs_are_registered():
    import ots.backup.jobs  # noqa: F401, PLC0415
    from ots.core.jobs import registered_jobs

    jobs = registered_jobs()
    assert (
        jobs["backup.enqueue_wal"].schedule == "continuous"
        and jobs["backup.rpo_monitor"].schedule == "* * * * *"
    )
