"""The Hub (M10 section 1): the live connections of this process, and the messages routed to them.

A message is a small JSON object with ids only, never text. It names who gets it: `users` (ids) and/or
`roles` (for example SUPER_ADMIN). A separate task listens on PostgreSQL (`LISTEN ots_live`) and hands each
notification to `route`, so the worker can reach every connected page through the database alone.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

import psycopg
from django.conf import settings

log = logging.getLogger(__name__)

CHANNEL = "ots_live"
MAX_PER_USER = 3
MAX_TOTAL = 100
QUEUE_SIZE = 100


class TooManyConnections(Exception):
    """This process already holds MAX_TOTAL connections (F-LV-2)."""


@dataclass(eq=False)
class Connection:
    user_id: str
    role: str
    session_key: str = ""
    queue: asyncio.Queue[dict[str, Any] | None] = field(default_factory=lambda: asyncio.Queue(QUEUE_SIZE))


class Hub:
    def __init__(self) -> None:
        self._by_user: dict[str, list[Connection]] = {}
        self._task: asyncio.Task[None] | None = None
        self.started_listener = False
        self.booted = False  # the start-up reset of live_connected has run in this process

    # ---- bookkeeping ----
    @property
    def total(self) -> int:
        return sum(len(v) for v in self._by_user.values())

    def full(self) -> bool:
        return self.total >= MAX_TOTAL

    def register(self, conn: Connection) -> list[Connection]:
        """Add a connection. Returns the ones evicted: a user may hold 3, the oldest is closed for a 4th."""
        if self.full():
            raise TooManyConnections
        mine = self._by_user.setdefault(conn.user_id, [])
        evicted: list[Connection] = []
        while len(mine) >= MAX_PER_USER:
            old = mine.pop(0)
            evicted.append(old)
            self._close(old)
        mine.append(conn)
        return evicted

    def unregister(self, conn: Connection) -> bool:
        """Remove a connection. True when the same login session has no other open connection left."""
        mine = self._by_user.get(conn.user_id, [])
        if conn in mine:
            mine.remove(conn)
        if not mine:
            self._by_user.pop(conn.user_id, None)
        return not any(c.session_key == conn.session_key for c in mine)

    @staticmethod
    def _close(conn: Connection) -> None:
        _put(conn, None)

    def connections(self, user_id: str) -> list[Connection]:
        return list(self._by_user.get(user_id, []))

    # ---- routing ----
    def route(self, message: dict[str, Any]) -> int:
        users = {str(u) for u in message.get("users", [])}
        roles = set(message.get("roles", []))
        body = {k: v for k, v in message.items() if k not in ("users", "roles")}
        sent = 0
        for uid, conns in list(self._by_user.items()):
            for c in conns:
                if uid in users or c.role in roles:
                    _put(c, body)
                    sent += 1
        return sent

    # ---- the PostgreSQL listener ----
    def ensure_started(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._listen())

    async def _listen(self) -> None:
        db = settings.DATABASES["default"]
        while True:
            try:
                conn = await psycopg.AsyncConnection.connect(
                    dbname=db["NAME"], user=db.get("USER") or None, password=db.get("PASSWORD") or None,
                    host=db.get("HOST") or None, port=db.get("PORT") or None, autocommit=True,
                )  # fmt: skip
                async with conn:
                    await conn.execute(f"LISTEN {CHANNEL}")
                    self.started_listener = True
                    # Messages sent while we were not listening are lost: tell every page to re-fetch.
                    self.route({"users": list(self._by_user), "type": "reload"})
                    async for note in conn.notifies():
                        try:
                            self.route(json.loads(note.payload))
                        except (ValueError, TypeError):
                            log.warning("bad live message", extra={"event": "live.bad_message"})
            except asyncio.CancelledError:
                raise
            except Exception:
                self.started_listener = False
                log.exception("live listener failed; retrying", extra={"event": "live.listener_failed"})
                await asyncio.sleep(2)


def _put(conn: Connection, message: dict[str, Any] | None) -> None:
    try:
        conn.queue.put_nowait(message)
    except asyncio.QueueFull:
        # A page that cannot keep up is told to re-fetch everything instead of receiving a partial story.
        while not conn.queue.empty():
            conn.queue.get_nowait()
        conn.queue.put_nowait(message if message is None else {"type": "reload"})


hub = Hub()
