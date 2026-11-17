"""Database connection hygiene for code that runs inside long-lived async responses.

Under ASGI every request runs its database work in a thread of its own, and Django keeps that thread's
connection open until the request ends. For a normal page that is milliseconds. For the live stream (open for
hours, one per browser tab) it would pin one database connection per tab while doing nothing, and PostgreSQL
only allows a few dozen. So a stream's short database calls go through `short_call`, which gives the
connection back at once.
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from typing import Any

from asgiref.sync import sync_to_async
from django.db import connections


def release_connections() -> None:
    """Close this thread's database connections, unless a transaction is open (closing would undo it)."""
    for conn in connections.all(initialized_only=True):
        if not conn.in_atomic_block:
            conn.close()


def short_call[T](fn: Callable[..., T]) -> Callable[..., Awaitable[T]]:
    """`await short_call(fn)(...)`: run a blocking database function, then free its connection."""

    @functools.wraps(fn)
    def run(*args: Any, **kwargs: Any) -> T:
        try:
            return fn(*args, **kwargs)
        finally:
            release_connections()

    return sync_to_async(run)
