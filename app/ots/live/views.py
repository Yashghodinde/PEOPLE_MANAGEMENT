"""GET /live/stream: one Server-Sent Events connection per open page."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from asgiref.sync import sync_to_async
from django.conf import settings
from django.http import HttpRequest, HttpResponse, StreamingHttpResponse

from ots.core.clock import now
from ots.core.idempotency import idempotency_exempt

from . import displays, liveness
from .hub import Connection, TooManyConnections, hub

PING_SECONDS = 25.0


def _frame(message: dict[str, Any]) -> bytes:
    name = str(message.get("type", "message"))
    return f"event: {name}\ndata: {json.dumps(message)}\n\n".encode()


@idempotency_exempt
async def stream(request: HttpRequest) -> HttpResponse:
    user = await request.auser()
    device = None
    if not user.is_authenticated:  # an office TV has no login: its pairing cookie is the credential
        device = await sync_to_async(displays.authenticate)(request.COOKIES.get(displays.COOKIE))
        if device is None:
            return HttpResponse(status=401)
    if hub.full():
        resp = HttpResponse("Too many open connections.", status=503)
        resp["Retry-After"] = "30"
        return resp
    session_key = request.session.session_key or "" if device is None else ""
    if device is not None:
        conn = Connection(user_id=f"display:{device.pk}", role="DISPLAY", session_key="")
    else:
        conn = Connection(user_id=str(user.pk), role=str(user.role), session_key=session_key)

    async def events() -> AsyncIterator[bytes]:
        try:
            hub.register(conn)
        except TooManyConnections:
            return
        try:
            if not hub.booted:
                hub.booted = True
                await liveness.boot_reset()
            hub.ensure_started()
            await liveness.connected(session_key)
            yield b"retry: 5000\n\n"
            yield _frame({"type": "hello", "server_time": now().isoformat(), "release": settings.OTS_VERSION})
            while True:
                try:
                    message = await asyncio.wait_for(conn.queue.get(), timeout=PING_SECONDS)
                except TimeoutError:
                    yield b": ping\n\n"
                    if device is not None:  # a page cannot see comments, so a TV also gets a real event
                        yield _frame({"type": "ping"})
                    continue
                if message is None:  # this connection was evicted by a newer one
                    return
                yield _frame(message)
        finally:
            if hub.unregister(conn):
                await liveness.disconnected(session_key)

    resp = StreamingHttpResponse(events(), content_type="text/event-stream")
    resp["Cache-Control"] = "no-cache"
    resp["X-Accel-Buffering"] = "no"
    return resp
