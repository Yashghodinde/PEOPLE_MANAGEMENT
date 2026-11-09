"""F-CORE-1: the same request sent 3x concurrently creates one row and returns 3 identical responses."""

import threading
import uuid

import pytest
from django.db import connection, connections
from django.test import Client

pytestmark = [pytest.mark.urls("tests.core.urls_test"), pytest.mark.django_db(transaction=True)]


def test_concurrent_duplicates_create_one_row():
    with connection.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS idem_test (id serial primary key, note text)")
        cur.execute("TRUNCATE idem_test")
    key = str(uuid.uuid4())
    results: list[tuple[int, bytes, str | None]] = []
    lock = threading.Lock()
    barrier = threading.Barrier(3)

    def worker():
        try:
            barrier.wait()
            r = Client().post("/api/create", HTTP_IDEMPOTENCY_KEY=key)
            with lock:
                results.append((r.status_code, r.content, r.headers.get("Idempotent-Replay")))
        finally:
            connections.close_all()

    threads = [threading.Thread(target=worker) for _ in range(3)]
    [t.start() for t in threads]
    [t.join(timeout=30) for t in threads]
    try:
        assert len(results) == 3
        assert {s for s, _, _ in results} == {200}
        assert len({body for _, body, _ in results}) == 1, "all three responses must be identical"
        assert sorted(f or "" for _, _, f in results) == ["", "true", "true"]
        with connection.cursor() as cur:
            cur.execute("SELECT count(*) FROM idem_test")
            assert cur.fetchone()[0] == 1
    finally:
        with connection.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS idem_test")
