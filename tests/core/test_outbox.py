import threading

import pytest
from django.db import DatabaseError, connection, transaction
from ots.core import dispatcher as dp
from ots.core import events
from ots.core.dispatcher import Dispatcher, Listener
from ots.core.models import OutboxEvent


@pytest.fixture(autouse=True)
def clean_registry():
    saved = {k: list(v) for k, v in events._HANDLERS.items()}
    wired = dp.alert_hook
    events._HANDLERS.clear()
    dp.alert_hook = None
    yield
    events._HANDLERS.clear()
    events._HANDLERS.update(saved)
    dp.alert_hook = wired


def make(type_="t.created", payload=None):
    with transaction.atomic():
        return events.publish(type_, ("thing", "abc"), payload or {"n": 1})


@pytest.mark.django_db
def test_publish_outside_atomic_raises():
    # pytest-django wraps the test in a transaction, so simulate "no atomic block"
    connection.in_atomic_block = False
    try:
        with pytest.raises(RuntimeError):
            events.publish("x", ("a", "1"))
    finally:
        connection.in_atomic_block = True


@pytest.mark.django_db
def test_publish_inserts_row_with_aggregate():
    row = make()
    assert OutboxEvent.objects.get(id=row.id).aggregate_type == "thing"
    assert row.published_at is None


@pytest.mark.django_db
def test_handler_runs_then_event_marked_published():
    seen = []
    events.subscribe("t.created")(lambda e: seen.append(e.id))
    row = make()
    assert Dispatcher().run_once() == 1
    assert seen == [row.id]
    assert OutboxEvent.objects.get(id=row.id).published_at is not None
    assert Dispatcher().run_once() == 0  # nothing left


@pytest.mark.django_db
def test_events_without_subscribers_are_still_marked_published():
    row = make("no.subscriber")
    Dispatcher().run_once()
    assert OutboxEvent.objects.get(id=row.id).published_at is not None


@pytest.mark.django_db
def test_handler_failure_leaves_unpublished_and_retries_with_backoff():
    calls, alerts, now = [], [], [0.0]
    dp.alert_hook = lambda *a: alerts.append(a)

    def flaky(e):
        calls.append(e.id)
        if len(calls) < 3:
            raise ValueError("boom")

    events.subscribe("t.created")(flaky)
    row = make()
    d = Dispatcher(clock=lambda: now[0])
    assert d.run_once() == 0 and OutboxEvent.objects.get(id=row.id).published_at is None
    assert alerts[0][0] == "outbox.handler_failed"
    assert d.run_once() == 0 and len(calls) == 1, "must wait for backoff"
    now[0] += 3
    assert d.run_once() == 0 and len(calls) == 2
    now[0] += 10
    assert d.run_once() == 1 and len(calls) == 3
    assert OutboxEvent.objects.get(id=row.id).published_at is not None


@pytest.mark.django_db
def test_dead_letter_after_ten_failures():
    alerts, now = [], [0.0]
    dp.alert_hook = lambda *a: alerts.append(a)

    def bad(e):
        raise RuntimeError("always")

    events.subscribe("t.created")(bad)
    row = make()
    d = Dispatcher(clock=lambda: now[0])
    for _ in range(dp.MAX_ATTEMPTS):
        d.run_once()
        now[0] += 1000
    ev = OutboxEvent.objects.get(id=row.id)
    assert ev.published_at is not None and ev.payload["dead_letter"] is True
    assert "RuntimeError" in ev.payload["dead_letter_reason"]
    assert alerts[-1][0] == "outbox.dead_letter" and alerts[-1][1] == "CRITICAL"


@pytest.mark.django_db
def test_update_and_delete_rejected_by_trigger_even_for_owner():
    row = make()
    with connection.cursor() as cur:
        with pytest.raises(DatabaseError), transaction.atomic():
            cur.execute("UPDATE outbox_events SET type = 'tampered' WHERE id = %s", [row.id])
        with pytest.raises(DatabaseError), transaction.atomic():
            cur.execute("DELETE FROM outbox_events WHERE id = %s", [row.id])


@pytest.mark.django_db
def test_ots_app_cannot_update_or_delete_but_can_insert_and_mark_published():
    with connection.cursor() as cur:
        cur.execute("SET ROLE ots_app")
        try:
            cur.execute(
                "INSERT INTO outbox_events (type, aggregate_type, aggregate_id, payload)"
                " VALUES ('x','a','1','{}') RETURNING id"
            )
            new_id = cur.fetchone()[0]
            for sql in ("UPDATE outbox_events SET type='y'", "DELETE FROM outbox_events"):
                with pytest.raises(DatabaseError), transaction.atomic():
                    cur.execute(sql)
            cur.execute("SELECT has_table_privilege('ots_app','outbox_events','TRUNCATE')")
            assert cur.fetchone()[0] is False
            cur.execute("SELECT ots_mark_published(%s)", [new_id])
            cur.execute("SELECT published_at IS NOT NULL FROM outbox_events WHERE id=%s", [new_id])
            assert cur.fetchone()[0] is True
        finally:
            cur.execute("RESET ROLE")


@pytest.mark.django_db(transaction=True)
def test_notify_fires_only_after_commit():
    listener = Listener()
    try:
        with transaction.atomic():
            events.publish("t.created", ("thing", "1"))
            assert listener.wait(timeout=0.5) is False  # not committed yet
        assert listener.wait(timeout=3) is True
    finally:
        listener.close()


@pytest.mark.django_db(transaction=True)
def test_rolled_back_publish_leaves_no_event_and_no_notify():
    listener = Listener()
    try:
        with pytest.raises(ZeroDivisionError), transaction.atomic():
            events.publish("t.created", ("thing", "1"))
            _ = 1 / 0
        assert OutboxEvent.objects.count() == 0 and listener.wait(timeout=0.5) is False
    finally:
        listener.close()


@pytest.mark.django_db(transaction=True)
def test_two_dispatchers_do_not_double_deliver():
    delivered, lock = [], threading.Lock()

    def h(e):
        with lock:
            delivered.append(e.id)

    events.subscribe("t.created")(h)
    for _ in range(20):
        make()

    def run():
        try:
            Dispatcher().run_once()
        finally:
            connection.close()

    ts = [threading.Thread(target=run) for _ in range(2)]
    [t.start() for t in ts]
    [t.join(timeout=30) for t in ts]
    assert sorted(delivered) == sorted(set(delivered)) and len(delivered) == 20
