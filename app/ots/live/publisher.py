"""The worker side: turn an event into a message on the live channel. Ids only; the page re-fetches itself."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from django.db import connection

from ots.core.events import Event, subscribe

from .hub import CHANNEL

MAX_PAYLOAD = 7000  # PostgreSQL allows 8000 bytes; keep clear of it


def send(users: Iterable[Any] = (), roles: Iterable[str] = (), type_: str = "reload", **data: Any) -> None:
    body = {"users": sorted({str(u) for u in users}), "roles": sorted(roles), "type": type_}
    body.update({k: str(v) for k, v in data.items()})
    payload = json.dumps(body)
    if len(payload.encode()) > MAX_PAYLOAD:
        payload = json.dumps({"users": [], "roles": ["SUPER_ADMIN"], "type": "reload"})
    with connection.cursor() as cur:
        cur.execute("SELECT pg_notify(%s, %s)", [CHANNEL, payload])


@subscribe("session.ended")
def session_ended(event: Event) -> None:
    """The server closed a session (idle, browser closed, forced): the open page shows the logged-out page."""
    send(users=[event.payload["user_id"]], type_="logged-out", reason=event.payload.get("reason", ""))
