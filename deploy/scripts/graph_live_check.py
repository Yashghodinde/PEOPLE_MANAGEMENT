#!/usr/bin/env python3
"""Live check of ots.storage.GraphProvider against the real tenant (M05.2 verification).

Works ONLY in the DR library, under a throw-away folder `_ots-live-test/<stamp>/`, and removes what it made.
It never touches Business-Files. Also records the QuickXorHash values Microsoft reports for deterministic
test data into tests/storage/vectors.json, so the offline tests can prove the hash implementation.

Run from the repo root with the GRAPH_* values exported (see .dev/etc/.env):
    set -a; . .dev/etc/.env; set +a
    OTS_STORAGE_BACKEND=graph uv run python deploy/scripts/graph_live_check.py [--no-vectors]
"""

import argparse
import datetime
import hashlib
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "app"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.test")

import django  # noqa: E402

django.setup()

from ots.storage.errors import GraphError, TransportFailure, WrongLibrary  # noqa: E402
from ots.storage.graph import GraphProvider  # noqa: E402
from ots.storage.provider import Library, RemoteRef  # noqa: E402
from ots.storage.quickxor import quickxor_b64  # noqa: E402

SIZES = [
    1,
    2,
    159,
    160,
    161,
    320,
    1000,
    4096,
    65536,
    1_000_000,
    4 * 1024 * 1024,
]  # the last is the 4 MiB boundary
BIG = 12 * 1024 * 1024
results: list[dict[str, object]] = []


def pattern(seed: str, size: int) -> bytes:
    """Deterministic, non-repeating test bytes: sha256(seed || counter) blocks. Same in tests/storage."""
    out = bytearray()
    i = 0
    while len(out) < size:
        out += hashlib.sha256(seed.encode() + i.to_bytes(8, "big")).digest()
        i += 1
    return bytes(out[:size])


class MemoryOp:
    def __init__(self, op_id: str) -> None:
        self.id = op_id
        self.upload_url = ""
        self.upload_expires_at: datetime.datetime | None = None
        self.confirmed_bytes = 0
        self.progress: list[int] = []

    def save_session(self, upload_url: str, expires_at: datetime.datetime | None) -> None:
        self.upload_url, self.upload_expires_at = upload_url, expires_at

    def save_progress(self, confirmed_bytes: int) -> None:
        self.confirmed_bytes = confirmed_bytes
        self.progress.append(confirmed_bytes)

    def clear_session(self) -> None:
        self.upload_url, self.upload_expires_at, self.confirmed_bytes = "", None, 0


class FlakyProvider(GraphProvider):
    """Real provider that 'loses the network' after N piece uploads, to prove a resume on the real service."""

    puts_allowed: int | None = None

    def _request(self, method, url, **kw):  # type: ignore[no-untyped-def]
        if method == "PUT" and kw.get("auth") is False and self.puts_allowed is not None:
            if self.puts_allowed == 0:
                raise TransportFailure("injected: network lost")
            self.puts_allowed -= 1
        return super()._request(method, url, **kw)


def check(name: str, ok: bool, note: str = "") -> bool:
    results.append({"check": name, "pass": ok, "note": note})
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {note}", flush=True)
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-vectors", action="store_true", help="do not (over)write tests/storage/vectors.json")
    ap.add_argument("--report", default=str(ROOT / ".dev" / "graph-live-check.json"))
    args = ap.parse_args()
    provider = FlakyProvider.from_settings()
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    base = f"_ots-live-test/{stamp}"
    tmp = ROOT / ".dev" / "live-check-tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    made: list[RemoteRef] = []
    quarantine_existed = provider.find_by_path(Library.DR, "_quarantine") is not None
    manifests_existed = provider.find_by_path(Library.DR, "Manifests") is not None
    vectors: list[dict[str, object]] = []

    def local(name: str, data: bytes) -> tuple[Path, int, str]:
        p = tmp / name
        p.write_bytes(data)
        return p, len(data), hashlib.sha256(data).hexdigest()

    try:
        left = (provider.certificate_not_after() - datetime.datetime.now(datetime.UTC)).days
        check("L1 certificate", left > 30, f"{left} days left")

        # L2: small uploads of many sizes; put() itself fails if Graph's hash differs from ours
        bad = []
        for size in SIZES:
            data = pattern(f"vec-{size}", size)
            p, n, sha = local(f"v{size}", data)
            try:
                ref = provider.put(Library.DR, f"{base}/v-{size}.bin", p, n, sha, MemoryOp(f"v{size}"))
                made.append(ref)
                reported = provider.head(ref).quick_xor_hash
                vectors.append({"seed": f"vec-{size}", "size": size, "quickxor": reported})
                if reported != quickxor_b64(data):
                    bad.append(size)
            except Exception as exc:  # noqa: BLE001
                bad.append(f"{size}:{type(exc).__name__}:{exc}")
        check("L2 small uploads + QuickXorHash equal to Graph's", not bad, f"{len(SIZES)} sizes; bad={bad}")

        # L3: large upload through an upload session
        big = pattern("big", BIG)
        pbig, nbig, shabig = local("big", big)
        op = MemoryOp("big-1")
        t0 = time.monotonic()
        ref_big = provider.put(Library.DR, f"{base}/big.bin", pbig, nbig, shabig, op)
        made.append(ref_big)
        check(
            "L3 large upload (12 MiB, session, verified)",
            True,
            f"{time.monotonic() - t0:.1f}s progress={op.progress}",
        )

        # L4: crash after 2 pieces, then resume with a NEW provider from the saved session
        big2 = pattern("big2", BIG)
        p2, n2, sha2 = local("big2", big2)
        op2 = MemoryOp("big-2")
        provider.puts_allowed = 2
        crashed = False
        try:
            provider.put(Library.DR, f"{base}/resume.bin", p2, n2, sha2, op2)
        except TransportFailure:
            crashed = True
        provider.puts_allowed = None
        saved = (op2.upload_url != "", op2.confirmed_bytes)
        again = GraphProvider.from_settings()
        ref2 = again.put(Library.DR, f"{base}/resume.bin", p2, n2, sha2, op2)
        made.append(ref2)
        check("L4 resume continues from the server's progress", crashed and saved[0] and saved[1] > 0,
              f"saved={saved} progress={op2.progress}")  # fmt: skip

        # L5: download matches byte for byte
        got = hashlib.sha256()
        total = 0
        for chunk in provider.open_read(ref_big):
            got.update(chunk)
            total += len(chunk)
        check("L5 download equals upload", got.hexdigest() == shabig and total == nbig, f"{total} bytes")

        # L6: same name again is refused (conflict=fail)
        try:
            provider.put(Library.DR, f"{base}/v-1.bin", *local("dup", b"x"), MemoryOp("dup"))
            check("L6 conflict is refused", False, "second upload was accepted")
        except GraphError as exc:
            check("L6 conflict is refused", exc.status == 409, f"{exc.status} {exc.code}")

        # L7: find_by_path and head agree
        found = provider.find_by_path(Library.DR, f"{base}/big.bin")
        head = provider.head(ref_big)
        same = bool(found) and found.ref.item_id == ref_big.item_id and found.path == f"{base}/big.bin"
        check("L7 find_by_path/head", same and head.size == nbig, f"path={found.path if found else None}")
        check("L7b absent path is None", provider.find_by_path(Library.DR, f"{base}/nope") is None)

        # L8: delta shows what we made, then only new things
        items, cursor = provider.changes(Library.DR, None)
        ids = {m.ref.item_id for m in items}
        check(
            "L8 delta lists our files",
            all(r.item_id in ids for r in made),
            f"{len(items)} items in the DR library",
        )
        p3, n3, sha3 = local("late", b"late file")
        late = provider.put(Library.DR, f"{base}/late.bin", p3, n3, sha3, MemoryOp("late"))
        made.append(late)
        time.sleep(2)
        more, cursor2 = provider.changes(Library.DR, cursor)
        check("L8b delta from cursor shows the new file", late.item_id in {m.ref.item_id for m in more}
              and cursor2 != cursor, f"{len(more)} changed")  # fmt: skip

        used, total_q = provider.usage()
        check("L9 usage", total_q > used > 0, f"{used / 1e6:.1f} MB of {total_q / 1e9:.1f} GB")

        # L10: the one allowed replace
        pm, nm, shm = local("m1", b"line 1\n")
        test_manifest = (
            "Manifests/0000-00.jsonl"  # the only name pattern replace is allowed for; deleted below
        )
        r1 = provider.replace_dr(test_manifest, pm, nm, shm)
        pm2, nm2, shm2 = local("m2", b"line 1\nline 2\n")
        r2 = provider.replace_dr(test_manifest, pm2, nm2, shm2)
        made.append(r2)
        content = b"".join(provider.open_read(r2))
        check(
            "L10 replace_dr replaces",
            content == b"line 1\nline 2\n",
            f"ids {r1.item_id[:6]} -> {r2.item_id[:6]}",
        )

        # L11: quarantine moves, never deletes
        pq, nq, shq = local("q", b"stray")
        stray = provider.put(Library.DR, f"{base}/stray.bin", pq, nq, shq, MemoryOp("stray"))
        moved = provider.move_to_quarantine(stray)
        where = provider.head(moved)
        made.append(moved)
        check("L11 quarantine moves the item", where.path.startswith("_quarantine/"), where.path)

        # L11b: the allowlist: nothing else can be replaced, and nothing is sent when it is refused
        try:
            provider.replace_dr(f"{base}/not-a-manifest.bin", pm, nm, shm)
            check("L11b replace refused outside the manifest allowlist", False)
        except WrongLibrary:
            check("L11b replace refused outside the manifest allowlist", True)

        # L12: the business library is protected before any network call
        try:
            provider.delete_dr(RemoteRef(Library.BUSINESS, "x", "y"))
            check("L12 business delete refused", False)
        except WrongLibrary:
            check("L12 business delete refused", True)
    finally:
        removed = 0
        for ref in made:
            try:
                provider.delete_dr(ref)
                removed += 1
            except Exception as exc:  # noqa: BLE001
                print(f"cleanup: could not delete {ref.item_id}: {exc}")
        # the throw-away folders (empty now)
        folders = (
            [base, "_ots-live-test"]
            + ([] if quarantine_existed else ["_quarantine"])
            + ([] if manifests_existed else ["Manifests"])
        )
        for folder in folders:
            meta = provider.find_by_path(Library.DR, folder)
            if meta is not None:
                try:
                    provider._request(
                        "DELETE",
                        f"https://graph.microsoft.com/v1.0/drives/{meta.ref.drive_id}/items/{meta.ref.item_id}",
                    )
                except GraphError as exc:
                    print(f"cleanup: folder {folder} left in place: {exc.status}")
        print(f"cleanup: removed {removed}/{len(made)} test files")
        for f in tmp.glob("*"):
            f.unlink()

    Path(args.report).write_text(json.dumps({"stamp": stamp, "results": results}, indent=2))
    if not args.no_vectors and all(r["pass"] for r in results if str(r["check"]).startswith("L2")):
        out = ROOT / "tests" / "storage" / "vectors.json"
        out.write_text(
            json.dumps({"generator": "sha256(seed || counter_be64) blocks", "vectors": vectors}, indent=2)
            + "\n"
        )
        print(f"wrote {out.relative_to(ROOT)} ({len(vectors)} vectors)")
    failed = [r for r in results if not r["pass"]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
