#!/usr/bin/env python3
"""Graph verification gate (P0-C): checks G1-G9 against the real tenant. Standalone: msal + httpx only.

Env: GRAPH_TENANT_ID, GRAPH_CLIENT_ID, GRAPH_CERT_PATH, GRAPH_KEY_PATH, GRAPH_SITE_ID,
     GRAPH_DRIVE_BUSINESS, GRAPH_DRIVE_DR.   Usage: graph_gate.py [--other-site-id ID] [--report PATH]
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import msal
from cryptography import x509
from cryptography.hazmat.primitives import hashes

GRAPH = "https://graph.microsoft.com/v1.0"
PIECE = 5 * 1024 * 1024
TOTAL = 30 * 1024 * 1024
E = os.environ.get


def token() -> str:
    cert = x509.load_pem_x509_certificate(Path(E("GRAPH_CERT_PATH", "")).read_bytes())
    thumb = cert.fingerprint(hashes.SHA1()).hex().upper()  # noqa: S303 (Entra thumbprint is SHA-1)
    app = msal.ConfidentialClientApplication(
        E("GRAPH_CLIENT_ID"),
        authority=f"https://login.microsoftonline.com/{E('GRAPH_TENANT_ID')}",
        client_credential={"private_key": Path(E("GRAPH_KEY_PATH", "")).read_text(), "thumbprint": thumb},
    )
    r = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
    if "access_token" not in r:
        raise RuntimeError(f"token failed: {r.get('error')} {r.get('error_description', '')[:200]}")
    return r["access_token"]


def req(client: httpx.Client, method: str, url: str, tries: int = 6, **kw) -> httpx.Response:
    for _ in range(tries):
        r = client.request(method, url, **kw)
        if r.status_code in (429, 503):
            time.sleep(min(int(r.headers.get("Retry-After", "5")), 60))
            continue
        return r
    return r


def gen_data() -> bytes:
    block = hashlib.sha256(b"ots-gate").digest()
    return (block * (TOTAL // len(block) + 1))[:TOTAL]


def worker(upload_url: str, state: str) -> None:
    """Upload pieces, printing a line after each. The parent kills us after piece 3."""
    data = gen_data()
    with httpx.Client(timeout=120) as c:
        for i in range(TOTAL // PIECE):
            lo, hi = i * PIECE, (i + 1) * PIECE - 1
            r = req(
                c,
                "PUT",
                upload_url,
                content=data[lo : hi + 1],
                headers={"Content-Range": f"bytes {lo}-{hi}/{TOTAL}", "Content-Length": str(PIECE)},
            )
            print(f"PIECE {i + 1} {r.status_code}", flush=True)
            if i + 1 == 3:
                time.sleep(3600)  # wait to be killed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--other-site-id", default="")
    ap.add_argument("--report", default="graph-gate-report.json")
    ap.add_argument("--worker", nargs=2, help=argparse.SUPPRESS)
    a = ap.parse_args()
    if a.worker:
        worker(*a.worker)
        return 0

    site, biz, dr = E("GRAPH_SITE_ID"), E("GRAPH_DRIVE_BUSINESS"), E("GRAPH_DRIVE_DR")
    host = (site or "").split(",")[0]
    other = a.other_site_id or host  # tenant root site is a different site
    results: list[dict] = []
    ctx: dict = {}

    def check(name: str, fn) -> None:  # type: ignore[no-untyped-def]
        t0 = time.time()
        try:
            ok, status, note = fn()
        except Exception as exc:  # noqa: BLE001
            ok, status, note = False, None, f"{type(exc).__name__}: {exc}"[:300]
        ms = int((time.time() - t0) * 1000)
        results.append({"check": name, "pass": ok, "status": status, "ms": ms, "note": note})
        print(f"{name}: {'PASS' if ok else 'FAIL'} status={status} {ms}ms {note}", flush=True)

    def g1():  # type: ignore[no-untyped-def]
        ctx["c"] = httpx.Client(
            headers={"Authorization": f"Bearer {token()}"}, timeout=120, follow_redirects=True
        )
        return True, 200, "token received"

    check("G1", g1)
    if "c" not in ctx:
        return finish(results, a.report)
    c: httpx.Client = ctx["c"]

    def g2():  # type: ignore[no-untyped-def]
        r = req(c, "GET", f"{GRAPH}/sites/{site}/drives")
        got = {d["id"]: d["name"] for d in r.json().get("value", [])} if r.status_code == 200 else {}
        ok = got.get(biz) == "Business-Files" and got.get(dr) == "System-DR"
        return ok, r.status_code, f"libraries: {sorted(got.values())}"

    def g3():  # type: ignore[no-untyped-def]
        r = req(c, "GET", f"{GRAPH}/sites/{other}")
        return r.status_code in (401, 403), r.status_code, f"other site {other[:60]}"

    def g4():  # type: ignore[no-untyped-def]
        r = req(
            c,
            "PUT",
            f"{GRAPH}/drives/{biz}/root:/_gate/small.txt:/content?@microsoft.graph.conflictBehavior=fail",
            content=b"x" * 1024,
        )
        return r.status_code == 201, r.status_code, ""

    def g5():  # type: ignore[no-untyped-def]
        r = req(
            c,
            "PUT",
            f"{GRAPH}/drives/{biz}/root:/_gate/small.txt:/content?@microsoft.graph.conflictBehavior=fail",
            content=b"x" * 1024,
        )
        return r.status_code == 409, r.status_code, ""

    def g6():  # type: ignore[no-untyped-def]
        r = req(
            c,
            "POST",
            f"{GRAPH}/drives/{dr}/root:/_gate/large.bin:/createUploadSession",
            json={"item": {"@microsoft.graph.conflictBehavior": "fail"}},
        )
        if r.status_code != 200:
            return False, r.status_code, "createUploadSession failed"
        url = r.json()["uploadUrl"]
        proc = subprocess.Popen(  # noqa: S603
            [sys.executable, __file__, "--worker", url, "-"],
            stdout=subprocess.PIPE,
            text=True,
            env=os.environ.copy(),
        )
        seen = 0
        for line in proc.stdout:  # type: ignore[union-attr]
            if line.startswith("PIECE"):
                seen += 1
            if seen == 3:
                break
        proc.kill()
        proc.wait()
        if seen != 3:
            return False, None, f"worker only reported {seen} pieces"
        s = httpx.get(url, timeout=60)  # upload URL needs no auth
        nxt = int(s.json()["nextExpectedRanges"][0].split("-")[0])
        data = gen_data()
        resp = None
        with httpx.Client(timeout=120) as up:
            while nxt < TOTAL:
                hi = min(nxt + PIECE, TOTAL) - 1
                resp = req(
                    up,
                    "PUT",
                    url,
                    content=data[nxt : hi + 1],
                    headers={"Content-Range": f"bytes {nxt}-{hi}/{TOTAL}"},
                )
                nxt = hi + 1
        ok = resp is not None and resp.status_code in (200, 201) and resp.json().get("size") == TOTAL
        ctx["large_id"] = resp.json().get("id") if resp is not None else None
        return (
            ok,
            resp.status_code if resp is not None else None,
            f"resumed from piece 4; size={resp.json().get('size') if resp is not None else '?'}",
        )

    def g7():  # type: ignore[no-untyped-def]
        r = req(c, "GET", f"{GRAPH}/drives/{dr}/items/{ctx['large_id']}/content")
        want = hashlib.sha256(gen_data()).hexdigest()
        got = hashlib.sha256(r.content).hexdigest()
        return r.status_code == 200 and want == got, r.status_code, f"sha256 {got[:12]}"

    def g8():  # type: ignore[no-untyped-def]
        url = f"{GRAPH}/drives/{dr}/root/delta"
        link = None
        while url:
            j = req(c, "GET", url).json()
            link = j.get("@odata.deltaLink") or link
            url = j.get("@odata.nextLink")
        if not link:
            return False, None, "no deltaLink"
        up = req(
            c,
            "PUT",
            f"{GRAPH}/drives/{dr}/root:/_gate/delta.txt:/content?@microsoft.graph.conflictBehavior=replace",
            content=b"delta",
        )
        names: list[str] = []
        for _ in range(6):  # delta can lag a few seconds
            time.sleep(3)
            url = link
            while url:
                j = req(c, "GET", url).json()
                names += [i.get("name", "") for i in j.get("value", [])]
                url = j.get("@odata.nextLink")
            if "delta.txt" in names:
                break
        return up.status_code in (200, 201) and "delta.txt" in names, up.status_code, "new item seen in delta"

    def g9():  # type: ignore[no-untyped-def]
        a_ = req(c, "DELETE", f"{GRAPH}/drives/{biz}/root:/_gate")
        b_ = req(c, "DELETE", f"{GRAPH}/drives/{dr}/root:/_gate")
        return a_.status_code == 204 and b_.status_code == 204, f"{a_.status_code}/{b_.status_code}", ""

    for n, f in (("G2", g2), ("G3", g3), ("G4", g4), ("G5", g5), ("G6", g6)):
        check(n, f)
    if ctx.get("large_id"):
        check("G7", g7)
    else:
        results.append({"check": "G7", "pass": False, "status": None, "ms": 0, "note": "skipped: G6 failed"})
        print("G7: FAIL (skipped, G6 failed)")
    check("G8", g8)
    check("G9", g9)  # always attempt cleanup
    return finish(results, a.report)


def finish(results: list[dict], path: str) -> int:
    Path(path).write_text(
        json.dumps({"results": results, "passed": all(r["pass"] for r in results)}, indent=2)
    )
    print(f"\nreport: {path}  overall: {'PASS' if all(r['pass'] for r in results) else 'FAIL'}")
    return 0 if all(r["pass"] for r in results) and len(results) == 9 else 1


if __name__ == "__main__":
    sys.exit(main())
