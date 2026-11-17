"""M04.2 end to end: a real PostgreSQL archives its WAL with the real script, the worker ships it through the real
queue into a (folder-backed) SharePoint, and what arrives decrypts to exactly what PostgreSQL wrote."""

import shutil
import socket
import subprocess
import time

import pytest
from ots.backup import monitor, shipper, wal
from ots.backup.models import BackupObject
from ots.storage.fake import LocalDirProvider
from ots.storage.models import StorageOperation as Op
from ots.storage.provider import Library

from tests.backup.test_shipper import _on_commit, dirs, handlers, pump  # noqa: F401
from tests.backup.test_wal_archive import SCRIPT, TOOLS, box, tool  # noqa: F401

PG = [shutil.which(t) for t in ("initdb", "pg_ctl", "psql")]
pytestmark = [
    pytest.mark.django_db,
    pytest.mark.skipif(not all(TOOLS) or not all(PG), reason="zstd, age or the PostgreSQL tools are missing"),
]


def test_wal_from_a_real_postgres_reaches_the_remote_in_order_and_decrypts(box, settings, tmp_path):  # noqa: F811
    settings.OTS_WAL_SPOOL_DIR = box["spool"]
    settings.OTS_WAL_LOCAL_DIR = box["local"]
    data = box["root"] / "cluster"
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    subprocess.run(
        [tool("initdb"), "-D", str(data), "-A", "trust", "-U", "postgres"], capture_output=True, check=True
    )
    script = (
        box["root"] / "ots-wal-archive.sh"
    )  # the repo path has spaces; PostgreSQL splits the command on them
    shutil.copy(SCRIPT, script)
    env = f"OTS_WAL_LOCAL={box['local']} OTS_WAL_SPOOL={box['spool']} OTS_AGE_PUB={box['pub']} OTS_DATA_DIR={box['root']}"
    opts = f"-p {port} -k {box['root']} -c wal_level=replica -c archive_mode=on -c archive_timeout=0 -c 'archive_command=env {env} {script} %p %f'"
    subprocess.run(
        [tool("pg_ctl"), "-D", str(data), "-o", opts, "-w", "start", "-l", str(box["root"] / "pg.log")],
        check=True,
        capture_output=True,
    )
    try:

        def sql(q):
            return subprocess.run(
                [tool("psql"), "-h", str(box["root"]), "-p", str(port), "-U", "postgres", "-Atc", q],
                capture_output=True, text=True, check=True,
            ).stdout.strip()  # fmt: skip

        sql("create table t(x int)")
        for i in range(4):
            sql(f"insert into t select generate_series(1,2000) + {i}")
            sql("select pg_switch_wal()")
        deadline = time.time() + 30
        while time.time() < deadline and int(sql("select archived_count from pg_stat_archiver")) < 4:
            time.sleep(0.4)
        assert int(sql("select archived_count from pg_stat_archiver")) >= 4
    finally:
        subprocess.run(
            [tool("pg_ctl"), "-D", str(data), "-m", "immediate", "stop"], capture_output=True, check=False
        )

    remote = LocalDirProvider(tmp_path / "sharepoint")
    assert shipper.enqueue_wal() >= 4
    pump(remote)
    assert not Op.objects.exclude(state=Op.State.DONE).exists()
    objs = list(BackupObject.objects.order_by("wal_segment"))
    assert len(objs) >= 4 and all(o.confirmed_at for o in objs)
    numbers = [wal.parse(o.wal_segment).number for o in objs if wal.parse(o.wal_segment)]
    assert wal.gaps(numbers) == []  # an unbroken chain
    # what is in "SharePoint" decrypts to exactly the plaintext copy the script kept locally
    for o in objs:
        meta = remote.find_by_path(Library.DR, o.remote_path)
        assert meta is not None and meta.size == o.size_bytes
        encrypted = b"".join(remote.open_read(meta.ref))
        plain = subprocess.run(
            [tool("age"), "-d", "-i", str(box["identity"])], input=encrypted, capture_output=True, check=True
        ).stdout
        arrived = subprocess.run([tool("zstd"), "-dc"], input=plain, capture_output=True, check=True).stdout
        kept = subprocess.run(
            [tool("zstd"), "-dc", str(box["local"] / f"{o.wal_segment}.zst")], capture_output=True, check=True
        ).stdout
        assert arrived == kept and len(arrived) == wal.SEGMENT_BYTES
    # nothing left to ship, and the recovery point is fresh
    assert list(box["spool"].glob("*.zst.age")) == []
    assert monitor.status().rpo_age is not None and monitor.status().rpo_age.total_seconds() < 120
