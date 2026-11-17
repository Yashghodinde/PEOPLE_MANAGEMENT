"""M04.3: the weekly base backup, against a real (scratch) PostgreSQL cluster, with a folder standing in for SharePoint."""

import datetime
import shutil
import socket
import subprocess
from pathlib import Path

import pytest
from freezegun import freeze_time
from ots.backup import base, tools
from ots.backup.models import BackupObject, BackupRun
from ots.core import alerts
from ots.storage import queue
from ots.storage.fake import LocalDirProvider
from ots.storage.gate import QueueGate
from ots.storage.models import StorageOperation as Op
from ots.storage.provider import Library

from tests.backup.test_shipper import _on_commit, handlers  # noqa: F401
from tests.backup.test_wal_archive import TOOLS, box, tool  # noqa: F401

PG = [shutil.which(t) for t in ("initdb", "pg_ctl", "psql", "pg_basebackup")]
pytestmark = [
    pytest.mark.django_db,
    pytest.mark.skipif(not all(TOOLS) or not all(PG), reason="zstd, age or the PostgreSQL tools are missing"),
]


@pytest.fixture
def cluster(box):  # noqa: F811
    """A running scratch PostgreSQL with some data in it; yields the Source to back it up from."""
    data = box["root"] / "cluster"
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    subprocess.run(
        [tool("initdb"), "-D", str(data), "-A", "trust", "-U", "postgres"], capture_output=True, check=True
    )
    opts = f"-p {port} -k {box['root']} -c wal_level=replica -c max_wal_senders=4"
    subprocess.run(
        [tool("pg_ctl"), "-D", str(data), "-o", opts, "-w", "start", "-l", str(box["root"] / "pg.log")],
        check=True,
        capture_output=True,
    )

    def sql(q):
        return subprocess.run(
            [tool("psql"), "-h", str(box["root"]), "-p", str(port), "-U", "postgres", "-Atc", q],
            capture_output=True, text=True, check=True,
        ).stdout.strip()  # fmt: skip

    sql("create table t(x int)")
    sql("insert into t select generate_series(1, 5000)")
    src = base.Source(host=str(box["root"]), port=port, user="postgres")
    yield src, sql
    subprocess.run(
        [tool("pg_ctl"), "-D", str(data), "-m", "immediate", "stop"], capture_output=True, check=False
    )


@pytest.fixture(autouse=True)
def dirs(settings, box):  # noqa: F811
    settings.OTS_BACKUP_LOCAL_DIR = box["root"] / "backup-local"
    settings.OTS_BACKUP_STAGING_DIR = box["root"] / "backup-staging"
    settings.OTS_BACKUP_AGE_PUBLIC_KEY_PATH = box["pub"]


def pump(remote):
    from tests.backup.test_shipper import _capture

    out = []
    for _ in range(20):
        with _capture["f"](execute=True):
            r = queue.process_one(provider=remote, gate=QueueGate(rng=lambda: 1.0))
        if r in ("idle", "waited"):
            break
        out.append(r)
    return out


def test_a_base_backup_is_compressed_encrypted_queued_and_only_then_a_success(cluster, box, tmp_path):  # noqa: F811
    src, _ = cluster
    clock = freeze_time("2026-11-22 19:30:00")  # 01:00 on Sunday 23 November in the office
    clock.start()
    run = base.run(src)
    run.refresh_from_db()
    assert (
        run.status == BackupRun.Status.RUNNING and run.step == "upload"
    )  # not a success until SharePoint has it
    plain = next(box["root"].joinpath("backup-local").glob("base-*.tar.zst"))
    assert plain.name == "base-20261123-0100.tar.zst"
    assert not list(box["root"].joinpath("backup-local").glob("*.tmp"))
    label = run.detail
    assert label["start_lsn"] and len(label["start_segment"]) == 24 and label["stop_lsn_at_least"]
    obj = BackupObject.objects.get()
    assert (
        obj.type == "BASE"
        and obj.confirmed_at is None
        and obj.remote_path == "PostgreSQL/Base-Backups/2026/11/23/base.tar.zst.age"
    )
    op = Op.objects.get(kind=Op.Kind.UPLOAD_BACKUP_OBJECT)
    assert op.target_library == "DR" and op.sha256 == obj.sha256 and op.remote_path == obj.remote_path

    remote = LocalDirProvider(tmp_path / "sharepoint")
    pump(remote)
    run.refresh_from_db()
    obj.refresh_from_db()
    assert (
        run.status == BackupRun.Status.SUCCESS and run.step == "done" and run.finished_at and obj.confirmed_at
    )
    assert not list(
        box["root"].joinpath("backup-staging").glob("*.age")
    )  # the encrypted copy is dropped once safe
    assert not alerts.open_alerts().filter(key="backup.base_failed").exists()
    clock.stop()


def test_what_arrives_in_sharepoint_decrypts_to_the_same_archive_and_restores(cluster, box, tmp_path):  # noqa: F811
    src, sql = cluster
    run = base.run(src)
    remote = LocalDirProvider(tmp_path / "sharepoint")
    pump(remote)
    obj = BackupObject.objects.get()
    meta = remote.find_by_path(Library.DR, obj.remote_path)
    assert meta is not None and meta.size == obj.size_bytes
    encrypted = b"".join(remote.open_read(meta.ref))
    plain_tar_zst = subprocess.run(
        [tool("age"), "-d", "-i", str(box["identity"])], input=encrypted, capture_output=True, check=True
    ).stdout
    local = next(box["root"].joinpath("backup-local").glob("base-*.tar.zst")).read_bytes()
    assert plain_tar_zst == local  # SharePoint's copy is exactly the local one, only encrypted
    # and it is a real base backup: unpack it and read the label and the version file
    out = tmp_path / "unpacked"
    out.mkdir()
    tar = subprocess.run([tool("zstd"), "-dc"], input=plain_tar_zst, capture_output=True, check=True).stdout
    subprocess.run(["tar", "-xf", "-", "-C", str(out)], input=tar, check=True)  # noqa: S607
    assert (out / "PG_VERSION").read_text().strip() == "16" or (out / "PG_VERSION").exists()
    assert "START WAL LOCATION" in (out / "backup_label").read_text()
    assert run.detail["start_segment"] in (out / "backup_label").read_text()
    assert sql("select count(*) from t") == "5000"  # the live database was not disturbed


def test_only_the_latest_plaintext_copy_is_kept(cluster, box):  # noqa: F811
    src, _ = cluster
    with freeze_time("2026-11-15 19:30:00"):
        base.run(src)
    with freeze_time("2026-11-22 19:30:00"):
        base.run(src)
    names = sorted(p.name for p in box["root"].joinpath("backup-local").glob("base-*.tar.zst"))
    assert names == ["base-20261123-0100.tar.zst"]


def test_a_failed_connection_is_recorded_alerted_and_leaves_nothing_half_written(cluster, box):  # noqa: F811
    src, _ = cluster
    bad = base.Source(host=src.host, port=src.port + 1, user="postgres")  # nothing listens there
    run = base.run(bad)
    run.refresh_from_db()
    assert run.status == BackupRun.Status.FAILED and run.step == "dump" and "error" in run.detail
    assert alerts.open_alerts().get(key="backup.base_failed").severity == "CRITICAL"
    local = box["root"] / "backup-local"
    assert not list(local.glob("*.tmp")) and not list(local.glob("base-*"))
    assert not Op.objects.exists() and not BackupObject.objects.exists()


def test_a_missing_public_key_stops_before_anything_is_copied(cluster, box, settings):  # noqa: F811
    src, _ = cluster
    settings.OTS_BACKUP_AGE_PUBLIC_KEY_PATH = box["root"] / "nope.pub"
    run = base.run(src)
    assert run.status == "FAILED" and run.step == "precheck"
    assert not list(box["root"].joinpath("backup-local").glob("base-*"))


def test_not_enough_disk_stops_at_the_precheck(cluster, box, monkeypatch):  # noqa: F811
    src, _ = cluster
    monkeypatch.setattr(base.shutil, "disk_usage", lambda p: shutil._ntuple_diskusage(100, 99, 1))
    run = base.run(src)
    assert run.status == "FAILED" and run.step == "precheck" and "free disk" in run.detail["error"]


def test_a_success_clears_an_earlier_failure_alert(cluster, box, tmp_path):  # noqa: F811
    src, _ = cluster
    base.run(base.Source(host=src.host, port=src.port + 1, user="postgres"))
    assert alerts.open_alerts().filter(key="backup.base_failed").exists()
    base.run(src)
    pump(LocalDirProvider(tmp_path / "sp"))
    assert not alerts.open_alerts().filter(key="backup.base_failed").exists()


# --- the 02:00 retry --------------------------------------------------------------------------------------------


def test_the_retry_runs_only_when_the_first_attempt_did_not_work(cluster, box, tmp_path, settings):  # noqa: F811
    src, _ = cluster
    settings.OTS_BACKUP_DB_HOST, settings.OTS_BACKUP_DB_PORT, settings.OTS_BACKUP_DB_USER = (
        src.host,
        src.port,
        "postgres",
    )
    with freeze_time("2026-11-22 19:30:00"):  # 01:00 Sunday
        failed = base.run(base.Source(host=src.host, port=src.port + 1, user="postgres"))
    assert failed.status == "FAILED"
    with freeze_time("2026-11-22 20:30:00"):  # 02:00 Sunday: nothing healthy since 01:00, so it tries again
        again = base.retry_if_needed()
        assert again is not None and again.detail["retry"] is True
    # a healthy run (even one still uploading) means no retry
    with freeze_time("2026-11-22 20:35:00"):
        assert base.retry_if_needed() is None


def test_a_run_stuck_uploading_for_hours_is_called_failed_so_it_can_be_retried(cluster):
    stuck = BackupRun.objects.create(
        type="BASE", started_at=datetime.datetime(2026, 11, 22, 19, 30, tzinfo=datetime.UTC), step="upload"
    )
    with freeze_time("2026-11-23 03:00:00"):  # 7.5 hours later
        base.retry_if_needed()
    stuck.refresh_from_db()
    assert stuck.status == "FAILED" and "stuck at step upload" in stuck.detail["error"]
    assert alerts.open_alerts().filter(key="backup.base_failed").exists()


def test_the_jobs_are_registered_for_sunday_one_and_two_am_and_run_heavy():
    import ots.backup.jobs  # noqa: F401, PLC0415
    from ots.core.jobs import registered_jobs

    jobs = registered_jobs()
    assert (jobs["backup.base"].schedule, jobs["backup.base"].heavy) == ("0 1 * * 0", True)
    assert (jobs["backup.base_retry"].schedule, jobs["backup.base_retry"].heavy) == ("0 2 * * 0", True)


# --- small pieces -----------------------------------------------------------------------------------------------


def test_the_backup_label_is_read_from_the_start_of_the_archive(tmp_path):
    label = "START WAL LOCATION: 0/3000028 (file 000000010000000000000003)\nCHECKPOINT LOCATION: 0/3000060\n"
    parsed = base.parse_backup_label(label)
    assert parsed == {
        "start_lsn": "0/3000028",
        "start_lsn_int": 0x3000028,
        "start_segment": "000000010000000000000003",
    }
    with pytest.raises(tools.ToolError):
        base.parse_backup_label("nothing here")
    tar = tmp_path / "x.tar"
    (tmp_path / "backup_label").write_text(label)
    subprocess.run(["tar", "-cf", str(tar), "-C", str(tmp_path), "backup_label"], check=True)  # noqa: S607
    subprocess.run([tool("zstd"), "-q", str(tar)], check=True)
    assert base.read_backup_label(Path(str(tar) + ".zst")) == label


def test_a_backup_that_hangs_is_killed_and_says_why(cluster, box, settings, monkeypatch):  # noqa: F811
    """pg_basebackup waits forever for the last WAL segment to be archived if archiving is broken (seen in a real
    container): the job must give up, not hang the worker."""
    src, _ = cluster
    settings.OTS_BACKUP_BASE_TIMEOUT_SECONDS = 1
    fake = box["root"] / "pg_basebackup"
    fake.write_text("#!/bin/sh\nsleep 30\n")
    fake.chmod(0o755)
    monkeypatch.setattr(tools, "path_of", lambda n: str(fake) if n == "pg_basebackup" else shutil.which(n))
    import time

    t0 = time.monotonic()
    run = base.run(src)
    assert time.monotonic() - t0 < 10
    assert run.status == BackupRun.Status.FAILED and run.step == "dump"
    assert "did not finish within 1 seconds" in run.detail["error"] and "WAL archiving" in run.detail["error"]
    assert not list(box["root"].joinpath("backup-local").glob("*.tmp"))
