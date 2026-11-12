"""F-ST-2: Microsoft throttles (429 with Retry-After: 120). The WHOLE queue pauses for 120 s, then continues."""

import datetime

import pytest
from freezegun import freeze_time
from ots.core import alerts
from ots.storage import classify as c
from ots.storage.fake import LocalDirProvider
from ots.storage.gate import QueueGate

from tests.storage.queue_stub import run_once

pytestmark = pytest.mark.django_db


def test_429_pauses_the_whole_queue_for_exactly_retry_after(tmp_path):
    provider = LocalDirProvider(tmp_path / "r")
    gate = QueueGate(rng=lambda: 1.0)
    with freeze_time("2026-11-12 06:00:00") as clock:
        provider.inject([429], retry_after=120)
        assert run_once(gate, provider) == "PAUSE_QUEUE"
        throttled = alerts.open_alerts().get(key=c.ALERT_THROTTLED)
        assert throttled.severity == "INFO"
        calls_before = len(provider.calls)
        for _ in range(5):  # any kind of operation: nothing reaches Microsoft during the pause
            for kind in ("ARCHIVE_FILE", "SHIP_WAL", "UPLOAD_BACKUP_OBJECT"):
                assert run_once(gate, provider, kind=kind) == "waited"
            clock.tick(datetime.timedelta(seconds=20))
        assert len(provider.calls) == calls_before  # 100 seconds in: still paused, no calls made
        clock.tick(datetime.timedelta(seconds=21))  # 121 s after the 429
        assert run_once(gate, provider) == "ok"
        assert not alerts.open_alerts().filter(key=c.ALERT_THROTTLED).exists()
