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


def test_at_95_percent_files_are_blocked_but_the_disaster_recovery_data_continues(tmp_path):
    """The other half of F-ST-4: not only a 507, but the capacity monitor's 95% line."""
    from ots.storage import capacity, queue
    from ots.storage.models import StorageOperation as Op
    from ots.storage.provider import Library

    from tests.storage.queue_helpers import make_op

    provider = LocalDirProvider(tmp_path / "r2")
    gate = QueueGate(rng=lambda: 1.0)
    provider.set_quota(10_000)
    provider.plant(Library.BUSINESS, "Files/ballast", b"x" * 9_600)  # 96% full
    reading = capacity.check_capacity(provider=provider, gate=gate)
    assert reading.archive_blocked and alerts.open_alerts().get(key=c.ALERT_CAPACITY).severity == "CRITICAL"
    file_op = make_op(tmp_path, b"new file", name="f", kind=Op.Kind.ARCHIVE_FILE)
    wal_op = make_op(tmp_path, b"wal", name="w", kind=Op.Kind.SHIP_WAL, library=Library.DR, remote="WAL/1")
    assert queue.process_one(provider=provider, gate=gate) == "done"  # the WAL goes through
    assert queue.process_one(provider=provider, gate=gate) == "idle"  # the file waits
    file_op.refresh_from_db()
    wal_op.refresh_from_db()
    assert (wal_op.state, file_op.state) == (Op.State.DONE, Op.State.PENDING)
