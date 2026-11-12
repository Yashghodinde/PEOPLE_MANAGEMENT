"""M05.3: the queue gate keeps blocks, pauses and the offline warning in the alerts table."""

import datetime

import pytest
from freezegun import freeze_time
from ots.core import alerts
from ots.storage import classify as c
from ots.storage.errors import GraphError, TransportFailure
from ots.storage.gate import ARCHIVE_KIND, BLOCK_KEY, Permission, QueueGate

pytestmark = pytest.mark.django_db
M = datetime.timedelta


def e(status, **kw):
    return GraphError(status, **kw)


def open_keys():
    return {a.key for a in alerts.open_alerts()}


@pytest.fixture
def gate():
    return QueueGate(rng=lambda: 1.0)


def test_nothing_is_blocked_at_first(gate):
    assert gate.permission().permission == Permission.RUN and gate.block() is None


def test_a_block_survives_a_restart(gate):
    with freeze_time("2026-11-12 06:00:00"):
        gate.failure(e(403))
        fresh = QueueGate()  # a new worker process
        assert fresh.block().state == "BLOCKED_AUTH"
        assert fresh.permission().permission == Permission.WAIT


def test_a_failed_probe_keeps_the_block_and_moves_the_next_probe(gate):
    with freeze_time("2026-11-12 06:00:00") as ft:
        gate.failure(e(403))
        since = gate.block().since
        ft.tick(M(minutes=15))
        assert gate.permission().permission == Permission.PROBE
        gate.failure(e(403), probe=True)
        assert gate.block().since == since  # same incident
        assert gate.permission().permission == Permission.WAIT
        ft.tick(M(minutes=14, seconds=59))
        assert gate.permission().permission == Permission.WAIT
        ft.tick(M(seconds=2))
        assert gate.permission().permission == Permission.PROBE


def test_a_probe_that_fails_for_another_reason_still_waits_15_minutes(gate):
    with freeze_time("2026-11-12 06:00:00") as ft:
        gate.failure(e(507))
        ft.tick(M(minutes=15))
        assert gate.permission().permission == Permission.PROBE
        gate.failure(e(500), probe=True)  # the internet blinked during the probe
        assert gate.permission().permission == Permission.WAIT and gate.block().state == "BLOCKED_QUOTA"


def test_a_successful_probe_clears_the_block_and_its_alerts(gate):
    with freeze_time("2026-11-12 06:00:00") as ft:
        gate.failure(e(403))
        assert {BLOCK_KEY, c.ALERT_AUTH} <= open_keys()
        ft.tick(M(minutes=15))
        gate.success(probe=True)
        assert gate.block() is None and not ({BLOCK_KEY, c.ALERT_AUTH} & open_keys())
        assert gate.permission().permission == Permission.RUN


def test_an_ordinary_success_while_blocked_does_not_clear_it(gate):
    with freeze_time("2026-11-12 06:00:00"):
        gate.failure(e(403))
        gate.success(probe=False)  # e.g. a status read; only a probe lifts the block
        assert gate.block() is not None


def test_retry_now_makes_the_next_operation_a_probe(gate):
    with freeze_time("2026-11-12 06:00:00"):
        gate.failure(e(403))
        assert gate.permission().permission == Permission.WAIT
        gate.retry_now()
        assert gate.permission().permission == Permission.PROBE


def test_throttle_pauses_everything_until_the_time_given_and_then_clears(gate):
    with freeze_time("2026-11-12 06:00:00") as ft:
        action = gate.failure(e(429, retry_after=120))
        assert action.kind == c.Kind.PAUSE_QUEUE and c.ALERT_THROTTLED in open_keys()
        d = gate.permission()
        assert d.permission == Permission.WAIT and d.wake_at == datetime.datetime.now(datetime.UTC) + M(
            seconds=120
        )
        ft.tick(M(seconds=119))
        assert gate.permission().permission == Permission.WAIT
        ft.tick(M(seconds=2))
        assert gate.permission().permission == Permission.RUN
        assert c.ALERT_THROTTLED not in open_keys()  # resolved by itself


def test_archive_only_block_lets_recovery_data_through(gate):
    with freeze_time("2026-11-12 06:00:00"):
        gate.block_archive_only("96% full")
        assert gate.permission(ARCHIVE_KIND).permission == Permission.WAIT
        assert gate.permission("SHIP_WAL").permission == Permission.RUN
        assert gate.permission("UPLOAD_BACKUP_OBJECT").permission == Permission.RUN
        gate.clear_block()
        assert gate.permission(ARCHIVE_KIND).permission == Permission.RUN


def test_offline_warning_appears_after_ten_minutes_without_a_success(gate):
    with freeze_time("2026-11-12 06:00:00") as ft:
        gate.failure(TransportFailure("down"))
        ft.tick(M(minutes=9, seconds=59))
        gate.failure(TransportFailure("down"))
        assert c.ALERT_OFFLINE not in open_keys()
        ft.tick(M(seconds=2))
        gate.failure(TransportFailure("down"))
        assert c.ALERT_OFFLINE in open_keys()
        gate.success()
        assert c.ALERT_OFFLINE not in open_keys()


def test_a_success_restarts_the_offline_clock(gate):
    with freeze_time("2026-11-12 06:00:00") as ft:
        gate.failure(TransportFailure("down"))
        ft.tick(M(minutes=8))
        gate.success()
        ft.tick(M(minutes=8))
        gate.failure(TransportFailure("down"))  # a fresh outage, 0 minutes old
        ft.tick(M(minutes=3))
        gate.failure(TransportFailure("down"))
        assert c.ALERT_OFFLINE not in open_keys()


def test_dead_letters_and_conflicts_raise_their_alerts(gate):
    with freeze_time("2026-11-12 06:00:00"):
        assert gate.failure(e(400, body="bad request")).kind == c.Kind.FAILED
        assert c.ALERT_DEAD_LETTER in open_keys()
        assert (
            gate.failure(e(409, code="nameAlreadyExists")).kind == c.Kind.CONFLICT
        )  # the queue decides: adopt or fail
        assert gate.block() is None


def test_the_first_attempt_time_decides_the_24_hour_give_up(gate):
    with freeze_time("2026-11-12 06:00:00") as ft:
        first = datetime.datetime.now(datetime.UTC)
        ft.tick(M(hours=24, seconds=1))
        a = gate.failure(e(502), attempts=50, first_attempt_at=first)
        assert a.kind == c.Kind.FAILED and c.ALERT_DEAD_LETTER in open_keys()
