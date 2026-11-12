"""F-ST-4: storage full (507). BLOCKED_QUOTA stops the whole queue with a CRITICAL alert and probes every 15 min."""

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


def test_507_blocks_the_queue_and_clears_when_space_returns(tmp_path):
    provider = LocalDirProvider(tmp_path / "r")
    gate = QueueGate(rng=lambda: 1.0)
    with freeze_time("2026-11-12 06:00:00") as clock:
        provider.inject([507])
        assert run_once(gate, provider) == "BLOCK_QUOTA"
        assert alerts.open_alerts().get(key=c.ALERT_QUOTA).severity == "CRITICAL"
        assert gate.block().state == "BLOCKED_QUOTA"
        for kind in ("ARCHIVE_FILE", "SHIP_WAL"):  # a 507 means everything is refused: the whole queue waits
            assert run_once(gate, provider, kind=kind) == "waited"
        clock.tick(M(minutes=15))
        assert run_once(gate, provider) == "ok"  # space was freed
        assert gate.block() is None
        assert not alerts.open_alerts().filter(key__in=[BLOCK_KEY, c.ALERT_QUOTA]).exists()
