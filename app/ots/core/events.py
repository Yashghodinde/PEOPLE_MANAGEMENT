"""Event outbox: publish inside the business transaction, dispatch after commit (F-CORE-3).

Delivery is at-least-once; handlers must be idempotent and receive the event id.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.db import connection, transaction

from .models import OutboxEvent

log = logging.getLogger(__name__)
NOTIFY_CHANNEL = "ots_events"


@dataclass(frozen=True)
class Event:
    id: int
    type: str
    aggregate_type: str
    aggregate_id: str
    payload: dict[str, Any]
    at: datetime


Handler = Callable[[Event], None]
_HANDLERS: dict[str, list[Handler]] = {}


def subscribe(event_type: str) -> Callable[[Handler], Handler]:
    def register(fn: Handler) -> Handler:
        _HANDLERS.setdefault(event_type, []).append(fn)
        return fn

    return register


def handlers_for(event_type: str) -> list[Handler]:
    return list(_HANDLERS.get(event_type, []))


def _aggregate(aggregate: Any) -> tuple[str, str]:
    if isinstance(aggregate, tuple):
        return str(aggregate[0]), str(aggregate[1])
    return aggregate._meta.model_name, str(aggregate.pk)


def publish(event_type: str, aggregate: Any, payload: dict[str, Any] | None = None) -> OutboxEvent:
    """Insert an outbox row. Must be called inside transaction.atomic()."""
    if not connection.in_atomic_block:
        raise RuntimeError("events.publish() must be called inside transaction.atomic()")
    agg_type, agg_id = _aggregate(aggregate)
    row = OutboxEvent.objects.create(
        type=event_type, aggregate_type=agg_type, aggregate_id=agg_id, payload=payload or {}
    )
    transaction.on_commit(lambda: _notify(row.id))
    return row


def _notify(event_id: int) -> None:
    with connection.cursor() as cur:
        cur.execute("SELECT pg_notify(%s, %s)", [NOTIFY_CHANNEL, str(event_id)])
