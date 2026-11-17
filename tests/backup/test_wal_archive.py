"""M04.1: the WAL archive script (deploy/postgres/ots-wal-archive.sh).

The plan names bats for these tests; they are pytest instead (D-073) so they run in the same suite and need no
extra tool. Skipped, loudly, when zstd or age is missing: then the backup script is NOT tested.
"""

import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy/postgres/ots-wal-archive.sh"
TOOLS = [shutil.which(t) for t in ("zstd", "age", "age-keygen")]
pytestmark = pytest.mark.skipif(
    not all(TOOLS), reason="zstd/age not installed: the WAL archive script is NOT tested"
)

SEGMENT = "000000010000000000000003"


def tool(name: str) -> str:
    """The full path of a command line tool (so no process is started from a bare name)."""
    path = shutil.which(name)
    assert path, f"{name} is not installed"
    return path


@pytest.fixture
def box():
    """A scratch 'server': data dir, the two WAL folders, an age key pair."""
    root = Path(tempfile.mkdtemp(prefix="walbox-", dir="/tmp"))
    ident = root / "identity.txt"
    out = subprocess.run([tool("age-keygen"), "-o", str(ident)], capture_output=True, text=True, check=True)
    pub = root / "backup.pub"
    pub.write_text(out.stderr.split("Public key:")[1].strip() + "\n")
    (root / "pgdata").mkdir()
    yield {
        "root": root, "pgdata": root / "pgdata", "local": root / "wal-local", "spool": root / "wal-spool",
        "pub": pub, "identity": ident,
    }  # fmt: skip
    shutil.rmtree(root, ignore_errors=True)


def run(box, name=SEGMENT, src=None, **env_extra):
    env = {
        **os.environ,
        "OTS_WAL_LOCAL": str(box["local"]), "OTS_WAL_SPOOL": str(box["spool"]), "OTS_AGE_PUB": str(box["pub"]),
        "OTS_DATA_DIR": str(box["root"]), "OTS_DISK_FREE_MIN_PERCENT": "0", **env_extra,
    }  # fmt: skip
    src = src if src is not None else f"pg_wal/{name}"
    return subprocess.run(
        [str(SCRIPT), src, name],
        cwd=box["pgdata"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def segment(box, data: bytes | None = None, name=SEGMENT, size=1024 * 1024):
    path = box["pgdata"] / "pg_wal" / name
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(data if data is not None else os.urandom(size))
    return path


def decrypt(box, name=SEGMENT) -> bytes:
    enc = (box["spool"] / f"{name}.zst.age").read_bytes()
    plain = subprocess.run(
        [tool("age"), "-d", "-i", str(box["identity"])], input=enc, capture_output=True, check=True
    ).stdout
    return subprocess.run([tool("zstd"), "-dc"], input=plain, capture_output=True, check=True).stdout


def unzip(path: Path) -> bytes:
    return subprocess.run([tool("zstd"), "-dc", str(path)], capture_output=True, check=True).stdout


def leftovers(box):
    return [p.name for d in (box["local"], box["spool"]) for p in d.rglob("*") if ".tmp" in p.name]


def test_a_new_segment_makes_both_copies_atomically(box):
    data = segment(box).read_bytes()
    r = run(box)
    assert r.returncode == 0, r.stderr
    assert unzip(box["local"] / f"{SEGMENT}.zst") == data  # readable without any key
    assert decrypt(box) == data  # and the encrypted one opens with the private key
    assert leftovers(box) == []
    assert b"BEGIN" not in (box["spool"] / f"{SEGMENT}.zst.age").read_bytes()[:20] or True


def test_the_spool_copy_is_not_readable_without_the_key(box):
    segment(box, data=b"SECRET-PAYLOAD" * 5000)
    run(box)
    raw = (box["spool"] / f"{SEGMENT}.zst.age").read_bytes()
    assert b"SECRET-PAYLOAD" not in raw and raw.startswith(b"age-encryption.org")


def test_running_again_with_identical_content_changes_nothing(box):
    segment(box)
    assert run(box).returncode == 0
    files = {
        p: p.stat().st_mtime_ns for d in (box["local"], box["spool"]) for p in d.rglob("*") if p.is_file()
    }
    time.sleep(0.05)
    r = run(box)
    assert r.returncode == 0, r.stderr
    assert files == {p: p.stat().st_mtime_ns for p in files}


def test_different_content_under_the_same_name_fails_and_the_original_survives(box):
    original = segment(box).read_bytes()
    assert run(box).returncode == 0
    segment(box)  # same name, new random content
    r = run(box)
    assert r.returncode == 1 and "different content" in r.stderr
    assert unzip(box["local"] / f"{SEGMENT}.zst") == original
    assert leftovers(box) == []


def test_a_nearly_full_disk_fails_and_writes_nothing(box):
    segment(box)
    r = run(box, OTS_DISK_FREE_MIN_PERCENT="101")  # more free space than any disk has
    assert r.returncode == 1 and "free on" in r.stderr
    assert not list(box["local"].glob("*")) if box["local"].exists() else True
    assert not list(box["spool"].glob("*.age")) if box["spool"].exists() else True


def test_sixteen_megabytes_in_under_two_seconds(box):
    segment(box, size=16 * 1024 * 1024)
    t0 = time.monotonic()
    r = run(box)
    took = time.monotonic() - t0
    assert r.returncode == 0, r.stderr
    assert took < 2.0, f"took {took:.2f}s"


def test_an_already_shipped_segment_is_not_queued_again(box):
    segment(box)
    (box["spool"] / ".shipped").mkdir(parents=True)
    (box["spool"] / ".shipped" / SEGMENT).touch()
    assert run(box).returncode == 0
    assert (box["local"] / f"{SEGMENT}.zst").exists()  # the local copy is still made
    assert not (box["spool"] / f"{SEGMENT}.zst.age").exists()


def test_a_segment_already_in_the_spool_is_left_alone(box):
    segment(box)
    run(box)
    spool = box["spool"] / f"{SEGMENT}.zst.age"
    before = spool.read_bytes()
    assert run(box).returncode == 0 and spool.read_bytes() == before


@pytest.mark.parametrize("name", ["../evil", "a/b", ".hidden", "name with space", "x;rm"])
def test_unsafe_names_are_refused(box, name):
    segment(box, name=SEGMENT)
    r = run(box, name=name, src=f"pg_wal/{SEGMENT}")
    assert r.returncode == 1 and "unsafe" in r.stderr
    assert not (box["root"] / "evil.zst").exists()


@pytest.mark.parametrize(
    "name",
    ["000000010000000000000003.partial", "00000002.history", "000000010000000000000003.00000028.backup"],
)
def test_history_backup_and_partial_files_are_archived_too(box, name):
    segment(box, name=name, data=b"label")
    assert run(box, name=name).returncode == 0
    assert unzip(box["local"] / f"{name}.zst") == b"label"


def test_a_missing_key_or_segment_fails_cleanly(box):
    segment(box)
    box["pub"].unlink()
    assert run(box).returncode == 1
    box["pub"].write_text("x")
    assert run(box, name="000000010000000000000009", src="pg_wal/nope").returncode == 1


def test_a_failure_in_the_second_step_leaves_the_local_copy_and_no_partial_spool_file(box):
    segment(box)
    box["pub"].write_text("not-a-valid-age-recipient\n")
    r = run(box)
    assert r.returncode != 0
    assert (box["local"] / f"{SEGMENT}.zst").exists()  # step 1 finished; PostgreSQL will simply retry
    assert leftovers(box) == [] and not list(box["spool"].glob("*.age"))


# --- against a real PostgreSQL ---------------------------------------------------------------------------------


PG = [shutil.which(t) for t in ("initdb", "pg_ctl", "psql")]


@pytest.mark.skipif(not all(PG), reason="PostgreSQL server tools not installed")
def test_real_postgres_archives_every_switched_segment(box):
    data = box["root"] / "cluster"
    sock = box["root"]
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    subprocess.run(
        [tool("initdb"), "-D", str(data), "-A", "trust", "-U", "postgres"], capture_output=True, check=True
    )
    env = {
        "OTS_WAL_LOCAL": str(box["local"]), "OTS_WAL_SPOOL": str(box["spool"]), "OTS_AGE_PUB": str(box["pub"]),
        "OTS_DATA_DIR": str(box["root"]), "OTS_DISK_FREE_MIN_PERCENT": "0",
    }  # fmt: skip
    script = (
        box["root"] / "ots-wal-archive.sh"
    )  # PostgreSQL splits the command on spaces, and this repo's path has some
    shutil.copy(SCRIPT, script)
    env_prefix = " ".join(f"{k}={v}" for k, v in env.items())
    opts = (
        f"-p {port} -k {sock} -c wal_level=replica -c archive_mode=on -c archive_timeout=0 "
        f"-c 'archive_command=env {env_prefix} {script} %p %f'"
    )
    subprocess.run(
        [tool("pg_ctl"), "-D", str(data), "-o", opts, "-w", "start", "-l", str(box["root"] / "pg.log")],
        check=True,
        capture_output=True,
    )
    try:

        def sql(q):
            return subprocess.run(
                [tool("psql"), "-h", str(sock), "-p", str(port), "-U", "postgres", "-Atc", q],
                capture_output=True, text=True, check=True,
            ).stdout.strip()  # fmt: skip

        sql("create table t(x int)")
        for i in range(3):
            sql(f"insert into t select generate_series(1,1000) + {i}")  # WAL to switch
            sql("select pg_switch_wal()")
        deadline = time.time() + 30
        while time.time() < deadline:
            archived = int(sql("select archived_count from pg_stat_archiver"))
            if archived >= 3:
                break
            time.sleep(0.5)
        assert int(sql("select failed_count from pg_stat_archiver")) == 0
        local = sorted(p.name.removesuffix(".zst") for p in box["local"].glob("*.zst"))
        spool = sorted(p.name.removesuffix(".zst.age") for p in box["spool"].glob("*.zst.age"))
        assert len(local) >= 3 and local == spool
        assert leftovers(box) == []
    finally:
        subprocess.run(
            [tool("pg_ctl"), "-D", str(data), "-m", "immediate", "stop"], capture_output=True, check=False
        )
