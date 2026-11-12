"""M10.1: routing, connection limits (F-LV-2) and the lag alert (F-LV-1)."""

import asyncio
import datetime

import pytest
from django.test import Client
from freezegun import freeze_time
from ots.core.alerts import open_alerts
from ots.core.events import publish
from ots.live import jobs
from ots.live.hub import MAX_PER_USER, MAX_TOTAL, Connection, Hub, TooManyConnections


def conn(user, role="EMPLOYEE", key=""):
    return Connection(user_id=user, role=role, session_key=key or user)


def drain(c):
    out = []
    while not c.queue.empty():
        out.append(c.queue.get_nowait())
    return out


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_a_user_only_receives_their_own_messages():
    h = Hub()
    a, b, boss = conn("a"), conn("b"), conn("boss", "SUPER_ADMIN")
    for c in (a, b, boss):
        h.register(c)
    assert h.route({"users": ["a"], "type": "task-changed", "task_id": "1"}) == 1
    assert drain(a) == [{"type": "task-changed", "task_id": "1"}] and drain(b) == [] and drain(boss) == []
    h.route({"roles": ["SUPER_ADMIN"], "type": "alert"})
    assert drain(boss) == [{"type": "alert"}] and drain(a) == []


def test_a_fourth_connection_closes_the_oldest():  # F-LV-2
    h = Hub()
    conns = [conn("a", key=f"s{i}") for i in range(MAX_PER_USER + 1)]
    evicted = [h.register(c) for c in conns]
    assert evicted[:3] == [[], [], []] and evicted[3] == [conns[0]]
    assert drain(conns[0]) == [None]  # told to close
    assert len(h.connections("a")) == MAX_PER_USER


def test_the_process_limit_refuses_more():  # F-LV-2
    h = Hub()
    for i in range(MAX_TOTAL):
        h.register(conn(f"u{i}"))
    with pytest.raises(TooManyConnections):
        h.register(conn("one-more"))
    assert h.full()


def test_a_session_counts_as_gone_only_when_its_last_tab_closes():
    h = Hub()
    t1, t2 = conn("a", key="same"), conn("a", key="same")
    h.register(t1)
    h.register(t2)
    assert h.unregister(t1) is False
    assert h.unregister(t2) is True


def test_a_page_that_cannot_keep_up_is_told_to_reload():
    h = Hub()
    c = conn("a")
    h.register(c)
    for i in range(150):
        h.route({"users": ["a"], "type": "task-changed", "n": str(i)})
    assert {"type": "reload"} in drain(c)


@pytest.mark.django_db
def test_a_stalled_dispatcher_raises_the_lag_alert():  # F-LV-1
    from django.db import transaction
    from django.utils import timezone

    with transaction.atomic():
        publish("noop.event", ("thing", "1"), {})  # its `at` is the database's real clock
    start = timezone.now()
    with freeze_time(start + datetime.timedelta(seconds=30)):
        assert jobs.check_lag() is False
        assert not any(a.key == "live.lag" for a in open_alerts())
    with freeze_time(start + datetime.timedelta(seconds=120)):
        assert jobs.check_lag() is True
        assert any(a.key == "live.lag" for a in open_alerts())


@pytest.mark.django_db
def test_the_stream_needs_a_login():
    assert Client().get("/live/stream").status_code == 401
