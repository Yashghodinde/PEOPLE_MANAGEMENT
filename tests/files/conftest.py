import hashlib
import io

import pytest
from django.test import Client

PW = "correct horse battery"


def login(login_id: str) -> Client:
    c = Client()
    assert c.post("/login", {"login_id": login_id, "password": PW}).status_code == 302
    return c


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def start(c: Client, data: bytes, key, task=None, name="report.xlsx", **extra):
    body = {
        "idempotency_key": str(key),
        "original_filename": name,
        "size": len(data),
        "sha256": sha(data),
        **extra,
    }
    if task is not None:
        body["task_id"] = str(task.pk)
    return c.post("/files/uploads", body, content_type="application/json", HTTP_ACCEPT="application/json")


def put(c: Client, upload_id, data: bytes, a: int, b: int):
    return c.put(
        f"/files/uploads/{upload_id}",
        data=io.BytesIO(data[a : b + 1]).getvalue(),
        content_type="application/octet-stream",
        HTTP_CONTENT_RANGE=f"bytes {a}-{b}/{len(data)}",
        HTTP_ACCEPT="application/json",
    )


def upload_all(c: Client, data: bytes, key, task=None, piece=1024, **kw):
    r = start(c, data, key, task, **kw)
    assert r.status_code in (200, 201), r.content
    uid = r.json()["upload_id"]
    a = r.json()["received_bytes"]
    last = None
    while a < len(data):
        b = min(a + piece, len(data)) - 1
        last = put(c, uid, data, a, b)
        assert last.status_code in (200, 201), last.content
        a = b + 1
    return uid, last


@pytest.fixture(autouse=True)
def _scratch_dirs(dirs):
    """Every test in tests/files writes under its own temporary directories."""
