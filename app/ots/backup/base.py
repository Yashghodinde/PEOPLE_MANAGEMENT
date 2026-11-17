"""The weekly base backup (M04 section 4): a full copy of the database cluster, the starting point for
restoring to any moment together with the WAL that follows it.

pg_basebackup runs over the network as the `ots_backup` role (REPLICATION only: it can read but not change
anything), is compressed on the fly, kept once in plaintext for restore tests, encrypted with age, and queued
for upload to SharePoint. The run is only SUCCESS once SharePoint has confirmed the encrypted copy.
"""

from __future__ import annotations

import datetime
import logging
import os
import re
import shutil
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from django.conf import settings
from django.db import transaction

from ots.core import alerts
from ots.core.clock import IST, now
from ots.storage import queue
from ots.storage.models import StorageOperation as Op
from ots.storage.provider import Library

from . import tools
from .models import BackupObject, BackupRun

log = logging.getLogger(__name__)
ALERT_KEY = "backup.base_failed"
MIN_FREE_BYTES = 1024**3  # when there is no earlier backup to size against
STALE_AFTER = datetime.timedelta(hours=6)


@dataclass(frozen=True)
class Source:
    """Where pg_basebackup connects. The password goes in the environment, never on a command line."""

    host: str
    port: int
    user: str
    password: str = ""

    @classmethod
    def from_settings(cls) -> Source:
        return cls(
            host=settings.OTS_BACKUP_DB_HOST,
            port=int(settings.OTS_BACKUP_DB_PORT),
            user=settings.OTS_BACKUP_DB_USER,
            password=settings.OTS_BACKUP_DB_PASSWORD,
        )


def local_dir() -> Path:
    return Path(settings.OTS_BACKUP_LOCAL_DIR)


def staging_dir() -> Path:
    return Path(settings.OTS_BACKUP_STAGING_DIR)


def public_key() -> Path:
    return Path(settings.OTS_BACKUP_AGE_PUBLIC_KEY_PATH)


_START = re.compile(r"START WAL LOCATION: ([0-9A-F]+)/([0-9A-F]+) \(file ([0-9A-F]{24})\)")


def parse_backup_label(text: str) -> dict[str, Any]:
    """START WAL LOCATION: 0/3000028 (file 000000010000000000000003) -> the start position and segment."""
    m = _START.search(text)
    if m is None:
        raise tools.ToolError("the backup label has no START WAL LOCATION line")
    high, low = int(m.group(1), 16), int(m.group(2), 16)
    return {
        "start_lsn": f"{m.group(1)}/{m.group(2)}",
        "start_lsn_int": (high << 32) | low,
        "start_segment": m.group(3),
    }


def read_backup_label(archive: Path) -> str:
    """backup_label is the first file in the archive, so this reads only the beginning of it."""
    proc = subprocess.Popen([tools.path_of("zstd"), "-dc", str(archive)], stdout=subprocess.PIPE)  # noqa: S603
    try:
        if proc.stdout is None:
            raise tools.ToolError("zstd gave no output")
        with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
            for member in tar:
                if member.name == "backup_label":
                    f = tar.extractfile(member)
                    if f is None:
                        raise tools.ToolError("backup_label is not a file")
                    return f.read().decode()
    finally:
        proc.kill()
        proc.wait()
    raise tools.ToolError("the base backup has no backup_label file")


def _stop_position(src: Source) -> str:
    """The write position just after pg_basebackup finished: at or beyond the real stop LSN of the backup.

    Informational only (the exact stop position lives in the .backup file archived beside the WAL), so a
    failure to ask is not a failure of the backup.
    """
    import psycopg

    try:
        with psycopg.connect(
            host=src.host, port=src.port, user=src.user, password=src.password or None, dbname="postgres",
            connect_timeout=10,
        ) as conn:  # fmt: skip
            row = conn.execute("SELECT pg_current_wal_lsn()::text").fetchone()
        return str(row[0]) if row else ""
    except psycopg.Error:
        return ""


def _precheck(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    previous = sorted(directory.glob("base-*.tar.zst"))
    need = previous[-1].stat().st_size * 2 if previous else MIN_FREE_BYTES
    free = shutil.disk_usage(directory).free
    if free < need:
        raise tools.ToolError(f"not enough free disk: {free // 1024**2} MB free, {need // 1024**2} MB needed")
    if not public_key().is_file():
        raise tools.ToolError(f"the backup public key is missing: {public_key()}")


def run(source: Source | None = None, at: datetime.datetime | None = None, retry: bool = False) -> BackupRun:
    """Take one base backup. An expected failure is recorded as FAILED with its step, not raised."""
    source = source or Source.from_settings()
    at = at or now()
    stamp = at.astimezone(IST)
    run_row = BackupRun.objects.create(
        type=BackupRun.Type.BASE, started_at=at, step="precheck", detail={"retry": retry}
    )
    plain = local_dir() / f"base-{stamp:%Y%m%d-%H%M}.tar.zst"
    tmp = plain.with_name(plain.name + ".tmp")
    try:
        _precheck(local_dir())
        staging_dir().mkdir(parents=True, exist_ok=True)

        _step(run_row, "dump")
        env = {**os.environ, "PGPASSWORD": source.password} if source.password else dict(os.environ)
        cmd = [
            tools.path_of("pg_basebackup"), "-h", source.host, "-p", str(source.port), "-U", source.user,
            "-D", "-", "-Ft", "-X", "none", "--checkpoint=fast", "--no-password",
        ]  # fmt: skip
        try:
            size = tools.pipe_to_zstd(
                cmd, tmp, env=env, timeout=float(settings.OTS_BACKUP_BASE_TIMEOUT_SECONDS)
            )
        except tools.ToolError as exc:
            if "did not finish" in str(exc):  # pg_basebackup waits for the last WAL segment to be archived
                raise tools.ToolError(f"{exc}. Is WAL archiving working? (see the archiver alert)") from exc
            raise
        label = parse_backup_label(read_backup_label(tmp))
        tmp.replace(plain)
        tools.fsync_dir(local_dir())
        for old in local_dir().glob("base-*.tar.zst"):  # only the latest plaintext copy is kept
            if old != plain:
                old.unlink(missing_ok=True)

        _step(run_row, "encrypt")
        encrypted = staging_dir() / f"{plain.name}.age"
        enc_size = tools.age_encrypt(plain, encrypted, public_key())
        digest = tools.sha256_of(encrypted)

        _step(run_row, "upload")
        remote = f"PostgreSQL/Base-Backups/{stamp:%Y/%m/%d}/base.tar.zst.age"
        with transaction.atomic():
            obj, _ = BackupObject.objects.update_or_create(
                remote_path=remote,
                defaults={
                    "run": run_row, "type": BackupObject.Type.BASE, "name": plain.name, "sha256": digest,
                    "size_bytes": enc_size, "confirmed_at": None, "pruned_at": None,
                },
            )  # fmt: skip
            queue.enqueue(
                Op.Kind.UPLOAD_BACKUP_OBJECT, Library.DR, encrypted, remote, digest, enc_size,
                ref_type="backup_run", ref_id=str(run_row.pk),
            )  # fmt: skip
            run_row.size_bytes = size
            run_row.detail = {
                **run_row.detail, **{k: v for k, v in label.items() if k != "start_lsn_int"},
                "stop_lsn_at_least": _stop_position(source), "plain_file": plain.name, "plain_bytes": size,
                "encrypted_bytes": enc_size, "object": str(obj.pk),
            }  # fmt: skip
            run_row.save(update_fields=["size_bytes", "detail"])
        return run_row
    except Exception as exc:  # noqa: BLE001  (every failure becomes a recorded, alerted state)
        tmp.unlink(missing_ok=True)
        fail(run_row, str(exc))
        return run_row


def _step(run_row: BackupRun, step: str) -> None:
    run_row.step = step
    run_row.save(update_fields=["step"])


def fail(run_row: BackupRun, message: str) -> None:
    run_row.status, run_row.finished_at = BackupRun.Status.FAILED, now()
    run_row.detail = {**run_row.detail, "error": message[:1500]}
    run_row.save(update_fields=["status", "finished_at", "detail"])
    log.error("base backup failed", extra={"event": "backup.base_failed", "step": run_row.step})
    alerts.raise_alert(
        ALERT_KEY, "CRITICAL", f"Weekly base backup failed at step {run_row.step}", message[:500]
    )


def on_uploaded(obj: BackupObject) -> None:
    """Called when SharePoint has confirmed the encrypted base backup: the run is now SUCCESS."""
    run_row = obj.run
    if run_row is None or run_row.type != BackupRun.Type.BASE or run_row.status != BackupRun.Status.RUNNING:
        return
    run_row.status, run_row.finished_at, run_row.step = BackupRun.Status.SUCCESS, now(), "done"
    run_row.save(update_fields=["status", "finished_at", "step"])
    alerts.resolve_alert(ALERT_KEY)
    encrypted = staging_dir() / f"{obj.name}.age"
    transaction.on_commit(lambda: encrypted.unlink(missing_ok=True))


def retry_if_needed(at: datetime.datetime | None = None) -> BackupRun | None:
    """The 02:00 retry: run again only if this week's base backup did not succeed."""
    at = at or now()
    # a run "uploading" for hours is stuck (the upload gave up): call it failed so it can be retried
    for stale in BackupRun.objects.filter(
        type=BackupRun.Type.BASE, status=BackupRun.Status.RUNNING, started_at__lt=at - STALE_AFTER
    ):
        fail(
            stale,
            f"stuck at step {stale.step} for more than {int(STALE_AFTER.total_seconds() // 3600)} hours",
        )
    since = at - datetime.timedelta(hours=12)
    healthy = BackupRun.objects.filter(
        type=BackupRun.Type.BASE,
        started_at__gte=since,
        status__in=[BackupRun.Status.SUCCESS, BackupRun.Status.RUNNING],
    ).exists()
    return None if healthy else run(retry=True, at=at)
