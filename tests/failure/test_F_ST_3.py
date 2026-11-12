"""F-ST-3: authorisation revoked (403). BLOCKED_AUTH stops the whole queue, a CRITICAL alert is raised, one probe
runs every 15 minutes, and the first successful probe clears the block."""

import datetime

import pytest
from freezegun import freeze_time
from ots.core import alerts
from ots.storage import classify as c
from ots.storage.fake import LocalDirProvider
from ots.storage.gate import BLOCK_KEY, QueueGate

from tests.storage.queue_stub import run_once

pytestmark = pytest.mark.django_db
M = datetime.timedelta


def test_403_blocks_probes_every_15_minutes_and_a_good_probe_clears_it(tmp_path):
    provider = LocalDirProvider(tmp_path / "r")
    gate = QueueGate(rng=lambda: 1.0)
    with freeze_time("2026-11-12 06:00:00") as clock:
        provider.inject([403])
        assert run_once(gate, provider) == "BLOCK_AUTH"
        assert alerts.open_alerts().get(key=c.ALERT_AUTH).severity == "CRITICAL"
        assert gate.block().state == "BLOCKED_AUTH"

        calls = len(provider.calls)
        for _ in range(14):  # 14 minutes: nothing is sent
            clock.tick(M(minutes=1))
            assert run_once(gate, provider) == "waited"
        assert len(provider.calls) == calls

        clock.tick(M(minutes=1))  # 15 minutes: exactly one probe, still revoked
        provider.inject([403])
        assert run_once(gate, provider) == "BLOCK_AUTH"
        assert len(provider.calls) == calls + 1
        assert run_once(gate, provider) == "waited"  # the next probe is 15 minutes away

        clock.tick(M(minutes=15))  # the admin fixed the permission: this probe succeeds
        assert run_once(gate, provider) == "ok"
        assert gate.block() is None
        assert not alerts.open_alerts().filter(key__in=[BLOCK_KEY, c.ALERT_AUTH]).exists()
        assert run_once(gate, provider) == "ok"  # the queue is running normally again
