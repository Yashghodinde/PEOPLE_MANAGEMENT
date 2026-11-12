"""F-ST-1: internet down. Operations wait and retry; after 10 minutes without a success a WARNING appears;
when the internet returns the next success resolves it. Nothing is lost."""

import datetime

import pytest
from freezegun import freeze_time
from ots.core import alerts
from ots.storage import classify as c
from ots.storage.fake import LocalDirProvider
from ots.storage.gate import QueueGate

from tests.storage.queue_stub import run_once

pytestmark = pytest.mark.django_db


def test_offline_for_ten_minutes_warns_and_recovery_resolves(tmp_path):
    provider = LocalDirProvider(tmp_path / "r")
    gate = QueueGate(rng=lambda: 1.0)
    with freeze_time("2026-11-12 06:00:00") as clock:
        provider.inject([502] * 40)  # every call fails: no connectivity
        assert run_once(gate, provider) == "RETRY"
        for _ in range(9):  # a tick a minute, 9 minutes
            clock.tick(datetime.timedelta(minutes=1))
            assert run_once(gate, provider) == "RETRY"
        assert not alerts.open_alerts().filter(key=c.ALERT_OFFLINE).exists()  # 9 minutes: not yet
        clock.tick(datetime.timedelta(minutes=1, seconds=1))
        run_once(gate, provider)
        offline = alerts.open_alerts().get(key=c.ALERT_OFFLINE)
        assert offline.severity == "WARNING"  # 10 minutes: now

        provider._failures.clear()  # the internet is back
        assert run_once(gate, provider) == "ok"
        assert not alerts.open_alerts().filter(key=c.ALERT_OFFLINE).exists()  # resolved by the first success
