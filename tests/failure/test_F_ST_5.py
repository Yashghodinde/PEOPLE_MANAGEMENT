"""F-ST-5: dead letter. Twenty-four hours of server errors end the retries: the operation is FAILED, a CRITICAL
alert is raised, and the local file stays pinned (it is the only copy we are sure of)."""

import datetime

import pytest
from freezegun import freeze_time
from ots.core import alerts
from ots.storage import classify as c
from ots.storage import queue
from ots.storage.gate import QueueGate
from ots.storage.models import StorageOperation as Op

from tests.storage.queue_helpers import make_op

pytestmark = pytest.mark.django_db


def test_24_hours_of_5xx_dead_letters_the_operation_and_keeps_the_local_file(fake, tmp_path):
    gate = QueueGate(rng=lambda: 1.0)
    op = make_op(tmp_path, b"important")
    fake.inject([502] * 500)
    with freeze_time("2030-01-01 06:00:00") as clock:
        steps = 0
        while Op.objects.get(pk=op.pk).state != Op.State.FAILED:
            queue.process_one(provider=fake, gate=gate)
            current = Op.objects.get(pk=op.pk)
            if current.state == Op.State.RETRY_WAIT:
                clock.move_to(current.next_attempt_at)  # jump to the next scheduled attempt
            steps += 1
            assert steps < 200, "never gave up"
        failed = Op.objects.get(pk=op.pk)
        elapsed = datetime.datetime.now(datetime.UTC) - failed.first_attempt_at
    assert elapsed >= datetime.timedelta(hours=24) and "gave up" in failed.last_error
    dead = alerts.open_alerts().get(key=c.ALERT_DEAD_LETTER)
    assert dead.severity == "CRITICAL"
    assert failed.local_path in queue.pinned_local_paths()  # still protected from cache eviction
    assert (tmp_path / "F-1.bin").read_bytes() == b"important"
    assert queue.process_one(provider=fake, gate=gate) == "idle"  # a dead letter is never retried by itself
