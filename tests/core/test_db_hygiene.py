"""A live stream must not pin a database connection for as long as it stays open (it filled PostgreSQL's limit)."""

import asyncio

import pytest
from django.conf import settings
from django.db import connection
from ots.core import db

pytestmark = pytest.mark.django_db


def test_connections_are_not_kept_open_between_requests_by_default():
    assert settings.DATABASES["default"]["CONN_MAX_AGE"] == 0


def test_release_leaves_an_open_transaction_alone():
    assert connection.in_atomic_block  # the test's own transaction
    db.release_connections()
    assert connection.connection is not None and connection.is_usable()  # not closed, not rolled back


@pytest.mark.django_db(transaction=True)
def test_a_short_call_gives_its_connection_back_when_it_is_done():
    from ots.accounts.models import User

    seen = {}

    def work():
        seen["n"] = User.objects.count()  # opens this thread's connection
        seen["open"] = connection.connection is not None
        return seen["n"]

    async def go():
        return await db.short_call(work)()

    asyncio.run(go())
    assert seen["open"] is True  # it did use one

    def check_closed():
        return connection.connection is None

    async def after():
        return await db.short_call(lambda: None)()

    # the same worker thread is reused by asgiref's executor for the next call; its connection was released
    assert asyncio.run(_state_after(work)) is True


async def _state_after(fn):
    from asgiref.sync import sync_to_async

    await db.short_call(fn)()
    return await sync_to_async(lambda: connection.connection is None)()
