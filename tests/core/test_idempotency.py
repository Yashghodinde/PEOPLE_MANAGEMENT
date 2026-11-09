import uuid
from datetime import timedelta

import pytest
from django.db import connection
from django.test import Client
from ots.core import clock
from ots.core.idempotency import purge_older_than_days
from ots.core.models import IdempotencyRecord

pytestmark = [pytest.mark.urls("tests.core.urls_test"), pytest.mark.django_db(transaction=True)]


@pytest.fixture(autouse=True)
def table():
    with connection.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS idem_test (id serial primary key, note text)")
        cur.execute("TRUNCATE idem_test")
    yield
    with connection.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS idem_test")


def rows():
    with connection.cursor() as cur:
        cur.execute("SELECT count(*) FROM idem_test")
        return cur.fetchone()[0]


def post(path, key=None, **kw):
    headers = {"HTTP_IDEMPOTENCY_KEY": str(key)} if key else {}
    return Client().post(path, **headers, **kw)


def test_missing_key_428():
    assert post("/api/create").status_code == 428 and rows() == 0


def test_malformed_key_400():
    r = Client().post("/api/create", HTTP_IDEMPOTENCY_KEY="not-a-uuid")
    assert r.status_code == 400 and rows() == 0


def test_replay_returns_same_response_and_flag():
    k = uuid.uuid4()
    a, b = post("/api/create", k), post("/api/create", k)
    assert a.status_code == b.status_code == 200 and a.content == b.content
    assert "Idempotent-Replay" not in a and b["Idempotent-Replay"] == "true"
    assert rows() == 1


def test_same_key_different_path_422():
    k = uuid.uuid4()
    assert post("/api/create", k).status_code == 200
    r = post("/api/other", k)
    assert r.status_code == 422 and r.json()["error"] == "idempotency_key_reused"
    assert rows() == 1


def test_4xx_is_stored_but_writes_roll_back():
    k = uuid.uuid4()
    a, b = post("/api/conflict", k), post("/api/conflict", k)
    assert a.status_code == b.status_code == 409 and b["Idempotent-Replay"] == "true"
    assert rows() == 0


def test_5xx_not_stored_and_rolled_back_so_client_can_retry():
    k = uuid.uuid4()
    assert post("/api/boom", k).status_code == 500
    assert not IdempotencyRecord.objects.filter(key=k).exists() and rows() == 0


def test_exempt_view_needs_no_key():
    assert post("/api/exempt").status_code == 200


def test_not_applied_outside_app_api_or_for_get():
    assert post("/plain/post").status_code == 200
    assert Client().get("/api/create").status_code == 200  # GET passes through the middleware


def test_purge_older_than_7_days():
    old, new = uuid.uuid4(), uuid.uuid4()
    for k in (old, new):
        IdempotencyRecord.objects.create(
            key=k, method="POST", path="/api/x", status_code=200, response_body=b""
        )
    IdempotencyRecord.objects.filter(key=old).update(created_at=clock.now() - timedelta(days=8))
    assert purge_older_than_days() == 1
    assert list(IdempotencyRecord.objects.values_list("key", flat=True)) == [new]
