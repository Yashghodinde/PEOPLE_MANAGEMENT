"""F-CORE-3: worker killed after the business commit but before dispatch.

On restart the event is dispatched exactly once (at-least-once delivery + idempotent handler).
"""

import pytest
from django.db import connection, transaction
from ots.core import events
from ots.core.dispatcher import Dispatcher
from ots.core.models import OutboxEvent

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def registry():
    saved = {k: list(v) for k, v in events._HANDLERS.items()}
    events._HANDLERS.clear()
    yield
    events._HANDLERS.clear()
    events._HANDLERS.update(saved)


def test_event_dispatched_once_after_crash_before_dispatch():
    applied: set[int] = set()  # idempotent handler: effect keyed by event_id
    deliveries: list[int] = []

    def handler(e):
        deliveries.append(e.id)
        applied.add(e.id)

    events.subscribe("task.assigned")(handler)

    with transaction.atomic():  # business commit succeeds ...
        row = events.publish("task.assigned", ("task", "t1"), {"to": "u1"})
    # ... and the worker dies here: nothing dispatched yet.
    assert OutboxEvent.objects.get(id=row.id).published_at is None and deliveries == []

    Dispatcher().run_once()  # restarted worker (fresh dispatcher, no memory of the past)
    Dispatcher().run_once()  # and another pass changes nothing
    assert deliveries == [row.id] and applied == {row.id}
    assert OutboxEvent.objects.get(id=row.id).published_at is not None


def test_crash_after_handler_but_before_mark_redelivers_and_handler_is_idempotent():
    applied: set[int] = set()
    deliveries: list[int] = []

    def handler(e):
        deliveries.append(e.id)
        applied.add(e.id)

    events.subscribe("task.assigned")(handler)
    with transaction.atomic():
        row = events.publish("task.assigned", ("task", "t1"))

    class Crash(Exception):
        pass

    d = Dispatcher()
    real_mark = d._mark

    def dying_mark(sql, params):
        raise Crash  # process dies after the handler ran, before the row is marked published

    d._mark = dying_mark  # type: ignore[method-assign]
    with pytest.raises(Crash):
        d.run_once()
    assert OutboxEvent.objects.get(id=row.id).published_at is None

    d2 = Dispatcher()
    d2._mark = real_mark  # type: ignore[method-assign]
    assert d2.run_once() == 1
    assert deliveries == [row.id, row.id]  # at-least-once
    assert applied == {row.id}  # idempotent effect: exactly once
    with connection.cursor() as cur:
        cur.execute("SELECT count(*) FROM outbox_events WHERE published_at IS NULL")
        assert cur.fetchone()[0] == 0
