"""Shared pieces for GraphProvider tests: a provider wired to respx, and Graph-shaped answers."""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any

import httpx
from ots.storage.graph import GraphProvider
from ots.storage.quickxor import quickxor_b64

G = "https://graph.microsoft.com/v1.0"
BIZ, DR = "drive-biz", "drive-dr"
UPLOAD_URL = "https://upload.sharepoint.example/session/abc123"


class Clock:
    """A monotonic clock that only moves when the provider 'sleeps'."""

    def __init__(self) -> None:
        self.t = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


def make_provider(
    clock: Clock | None = None, tokens: list[str] | None = None
) -> tuple[GraphProvider, Clock, list[bool]]:
    clock = clock or Clock()
    issued: list[bool] = []
    queue = list(tokens or [])

    def token_fn(force: bool) -> tuple[str, int]:
        issued.append(force)
        return (queue.pop(0) if queue else "tok", 3600)

    p = GraphProvider(
        tenant_id="t", client_id="c", cert_path=Path("/nope"), key_path=Path("/nope"), site_id="s",
        drive_business=BIZ, drive_dr=DR, client=httpx.Client(), token_fn=token_fn,
        sleep=clock.sleep, monotonic=clock,
    )  # fmt: skip
    return p, clock, issued


def item(
    data: bytes | None = None, *, size: int | None = None, qx: str | None = "auto", name: str = "F-1",
    folder: str = "/drives/drive-biz/root:/Files/2026/11", item_id: str = "item-1", drive: str = BIZ,
) -> dict[str, Any]:  # fmt: skip
    """A driveItem as Graph returns it."""
    body: dict[str, Any] = {
        "id": item_id,
        "name": name,
        "eTag": '"{etag},1"',
        "size": size if size is not None else len(data or b""),
        "lastModifiedDateTime": "2026-11-12T06:00:00Z",
        "parentReference": {"driveId": drive, "path": folder},
        "file": {"hashes": {}},
    }
    if qx == "auto" and data is not None:
        body["file"]["hashes"]["quickXorHash"] = quickxor_b64(data)
    elif qx not in (None, "auto"):
        body["file"]["hashes"]["quickXorHash"] = qx
    return body


def now_plus(minutes: int) -> str:
    return (datetime.datetime.now(datetime.UTC) + datetime.timedelta(minutes=minutes)).isoformat()
