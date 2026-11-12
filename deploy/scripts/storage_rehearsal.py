#!/usr/bin/env python3
"""Rehearse the real storage chain end to end: real queue, real database, real Graph (System-DR only).

It exists because the M05 pieces were tested separately (the Graph provider live, the queue against a
fake). This runs them together, and lets a person do the one thing a script cannot: delete a file in the
SharePoint web page.

    up [--size-mb N]   upload a file through the queue, then take the baseline reconciliation
    check              (after you deleted it in SharePoint) reconcile, then let the queue repair it
    status             what the database and SharePoint hold right now
    cleanup            remove everything this rehearsal made from System-DR

Everything lives under System-DR/_ots-rehearsal/. Business-Files is never touched. Needs DATABASE_URL
pointing at a scratch database, the GRAPH_* values, and OTS_STORAGE_BACKEND=graph.
"""

import argparse
import hashlib
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "app"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.test")
os.environ.setdefault("OTS_STORAGE_BACKEND", "graph")
KEYFILE = ROOT / ".dev" / "etc" / "rehearsal-field.key"
if (
    "OTS_FIELD_ENCRYPTION_KEY" not in os.environ
):  # the upload URL is stored encrypted: keep one key between runs
    from cryptography.fernet import Fernet

    KEYFILE.parent.mkdir(parents=True, exist_ok=True)
    if not KEYFILE.exists():
        KEYFILE.write_text(Fernet.generate_key().decode())
        KEYFILE.chmod(0o600)
    os.environ["OTS_FIELD_ENCRYPTION_KEY"] = KEYFILE.read_text().strip()

import django  # noqa: E402

django.setup()

from django.core.management import call_command  # noqa: E402
from ots.core import alerts  # noqa: E402
from ots.storage import queue, reconcile  # noqa: E402
from ots.storage.factory import get_provider  # noqa: E402
from ots.storage.handlers import register_completion_handler  # noqa: E402
from ots.storage.models import StorageOperation as Op  # noqa: E402
from ots.storage.provider import Library, RemoteRef  # noqa: E402

FOLDER = "_ots-rehearsal"
WORK = ROOT / ".dev" / "rehearsal"


def say(msg: str) -> None:
    print(msg, flush=True)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def make_file(name: str, size_mb: int) -> Path:
    WORK.mkdir(parents=True, exist_ok=True)
    path = WORK / name
    if size_mb <= 0:
        path.write_text(
            "OTS rehearsal file.\nIf you can read this in System-DR, the upload worked.\n"
            "Delete it in the SharePoint web page to test the repair.\n" + "x" * 2000 + "\n"
        )
    else:
        with path.open("wb") as fh:  # random data, written in 1 MiB pieces so memory stays flat
            for _ in range(size_mb):
                fh.write(os.urandom(1024 * 1024))
    return path


def drain(provider, label: str) -> int:
    """Run the queue until nothing is due. Returns how many operations it finished."""
    done = 0
    while True:
        t0 = time.monotonic()
        result = queue.process_one(provider=provider)
        if result in ("idle", "waited"):
            return done
        say(f"  queue: {result} ({time.monotonic() - t0:.1f}s) {label}")
        done += 1


def show_ops() -> None:
    for op in Op.objects.order_by("created_at"):
        line = f"  {op.kind:13} {op.state:10} remote={op.remote_state:10}"
        say(f"{line} {op.remote_path}  attempts={op.attempts}")


def show_alerts() -> None:
    open_alerts = list(alerts.open_alerts())
    if not open_alerts:
        say("  (no open alerts)")
    for a in open_alerts:
        say(f"  [{a.severity}] {a.key}: {a.title}")


def cmd_up(args: argparse.Namespace) -> int:
    provider = get_provider()
    name = (
        args.name or f"rehearsal-{time.strftime('%H%M%S')}.txt"
        if args.size_mb <= 0
        else f"big-{args.size_mb}MB.bin"
    )
    path = make_file(name, args.size_mb)
    size = path.stat().st_size
    say(f"file: {path.name}  {size:,} bytes")
    seen: dict[str, object] = {}
    original = Op.save_session

    def spy(self, upload_url, expires_at):  # type: ignore[no-untyped-def]
        seen["expires_at"] = expires_at
        original(self, upload_url, expires_at)

    Op.save_session = spy  # type: ignore[method-assign]
    register_completion_handler(
        Op.Kind.ARCHIVE_FILE, lambda op, ref: say(f"  handler: {op.remote_path} recorded as archived")
    )
    register_completion_handler(
        Op.Kind.REPAIR_FILE, lambda op, ref: say(f"  handler: {op.remote_path} recorded as REPAIRED")
    )
    op = queue.enqueue(
        Op.Kind.ARCHIVE_FILE,
        Library.DR,
        path,
        f"{FOLDER}/{name}",
        sha256_of(path),
        size,
        ref_type="rehearsal",
        ref_id=name,
    )
    t0 = time.monotonic()
    drain(provider, name)
    op.refresh_from_db()
    elapsed = time.monotonic() - t0
    say(f"upload: {op.state} in {elapsed:.1f}s ({size / 1e6 / max(elapsed, 0.001) * 8:.1f} Mbit/s)")
    if "expires_at" in seen:
        import datetime

        left = seen["expires_at"] - datetime.datetime.now(datetime.UTC)  # type: ignore[operator]
        say(f"upload session lifetime reported by Microsoft: {left.total_seconds() / 3600:.1f} hours")
    if op.state != Op.State.DONE:
        say(f"NOT DONE: {op.last_error}")
        return 1
    meta = provider.find_by_path(Library.DR, op.remote_path)
    say(
        f"SharePoint holds: {meta.path}  {meta.size:,} bytes  item {meta.ref.item_id[:12]}..."
        if meta
        else "NOT FOUND"
    )
    stats = reconcile.run(provider=provider)
    say(f"baseline reconciliation: {dict(stats) or 'nothing to report'}")
    say("")
    say(f"NOW: open System-DR in the browser -> folder {FOLDER} -> delete  {name}")
    say("THEN tell me, and run:  storage_rehearsal.py check")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    provider = get_provider()
    deadline = time.monotonic() + args.wait
    found = 0
    while True:
        stats = reconcile.run(provider=provider)
        found = stats.get("missing", 0)
        say(f"reconciliation: {dict(stats) or 'nothing to report'}")
        if found or time.monotonic() >= deadline:
            break
        say("  not seen yet; SharePoint's change feed can lag a little. Trying again in 15 s...")
        time.sleep(15)
    if not found:
        say("RESULT: the deletion was NOT detected.")
        return 1
    say("detected. alerts:")
    show_alerts()
    say("running the queue (the repair upload):")
    drain(provider, "repair")
    show_ops()
    ok = True
    for op in Op.objects.filter(kind=Op.Kind.REPAIR_FILE, state=Op.State.DONE):
        meta = provider.find_by_path(Library.DR, op.remote_path)
        if meta is None:
            ok = False
            say(f"  MISSING AFTER REPAIR: {op.remote_path}")
            continue
        h = hashlib.sha256()
        for chunk in provider.open_read(meta.ref):
            h.update(chunk)
        same = h.hexdigest() == op.sha256
        ok = ok and same
        say(f"  restored: {meta.path}  {meta.size:,} bytes  content identical to the original: {same}")
    failed = Op.objects.filter(state=Op.State.FAILED)
    for op in failed:
        ok = False
        say(f"  FAILED: {op.kind} {op.remote_path}: {op.last_error}")
    say(
        "RESULT: "
        + (
            "detected, alerted and repaired from the cache."
            if ok
            else "detected, but the repair did not complete."
        )
    )
    return 0 if ok else 1


def cmd_status(args: argparse.Namespace) -> int:
    provider = get_provider()
    say("operations:")
    show_ops()
    say("alerts:")
    show_alerts()
    items, _ = provider.changes(Library.DR, None)
    say("System-DR now holds (rehearsal folder):")
    for m in items:
        if m.path.startswith(FOLDER):
            say(f"  {m.path}  {m.size:,} bytes")
    return 0


def cmd_cleanup(args: argparse.Namespace) -> int:
    provider = get_provider()
    removed = 0
    for op in Op.objects.filter(state=Op.State.DONE).exclude(item_id=""):
        try:
            provider.delete_dr(RemoteRef(Library(op.target_library), op.drive_id, op.item_id))
            removed += 1
        except Exception as exc:  # noqa: BLE001  (already deleted in the web page, for instance)
            say(f"  skip {op.remote_path}: {getattr(exc, 'status', exc)}")
    meta = provider.find_by_path(Library.DR, FOLDER)
    if meta is not None:
        try:
            provider._request(
                "DELETE",
                f"https://graph.microsoft.com/v1.0/drives/{meta.ref.drive_id}/items/{meta.ref.item_id}",
            )  # type: ignore[attr-defined]
            say(f"  removed the folder {FOLDER}")
        except Exception as exc:  # noqa: BLE001
            say(f"  folder left in place: {getattr(exc, 'status', exc)}")
    for f in WORK.glob("*") if WORK.exists() else []:
        f.unlink()
    say(f"cleanup: removed {removed} file(s) from System-DR")
    return 0


def main() -> int:
    call_command("migrate", verbosity=0)
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    up = sub.add_parser("up")
    up.add_argument("--size-mb", type=int, default=0, help="0 = a tiny text file")
    up.add_argument("--name", default="")
    chk = sub.add_parser("check")
    chk.add_argument("--wait", type=int, default=120, help="seconds to keep looking for the deletion")
    sub.add_parser("status")
    sub.add_parser("cleanup")
    args = ap.parse_args()
    return {"up": cmd_up, "check": cmd_check, "status": cmd_status, "cleanup": cmd_cleanup}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
