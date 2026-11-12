"""F-ST-7: name conflict (409). Same size and hash: it is our own earlier upload, so adopt it. Different content:
FAILED (dead letter) plus a CRITICAL storage.conflict alert. Nothing is ever overwritten."""

import pytest
from ots.core import alerts
from ots.storage import classify as c
from ots.storage import queue
from ots.storage.gate import QueueGate
from ots.storage.models import StorageOperation as Op
from ots.storage.provider import Library

from tests.storage.queue_helpers import make_op

pytestmark = pytest.mark.django_db


def test_a_conflict_with_identical_content_is_adopted_not_failed(fake, tmp_path):
    gate = QueueGate(rng=lambda: 1.0)
    op = make_op(tmp_path, b"identical")
    existing = fake.plant(Library.BUSINESS, op.remote_path, b"identical")
    assert queue.process_one(provider=fake, gate=gate) == "adopted"
    op.refresh_from_db()
    assert op.state == Op.State.DONE and op.item_id == existing.item_id
    assert not alerts.open_alerts().filter(key=c.ALERT_CONFLICT).exists()


def test_a_conflict_with_different_content_is_a_dead_letter_with_a_critical_alert(fake, tmp_path):
    gate = QueueGate(rng=lambda: 1.0)
    op = make_op(tmp_path, b"our content")
    stranger = fake.plant(Library.BUSINESS, op.remote_path, b"someone elses!")
    assert queue.process_one(provider=fake, gate=gate) == "failed"
    op.refresh_from_db()
    assert op.state == Op.State.FAILED and "different content" in op.last_error
    assert alerts.open_alerts().get(key=c.ALERT_CONFLICT).severity == "CRITICAL"
    assert b"".join(fake.open_read(stranger)) == b"someone elses!"  # the stranger's file was not touched
    assert op.local_path in queue.pinned_local_paths()


def test_same_size_but_different_bytes_is_not_adopted(fake, tmp_path):
    gate = QueueGate(rng=lambda: 1.0)
    op = make_op(tmp_path, b"AAAAAAAA")
    fake.plant(Library.BUSINESS, op.remote_path, b"BBBBBBBB")  # same length, different content
    assert queue.process_one(provider=fake, gate=gate) == "failed"


def test_the_hash_check_falls_back_to_downloading_when_graph_gave_no_hash(fake, tmp_path):
    gate = QueueGate(rng=lambda: 1.0)
    op = make_op(tmp_path, b"no hash reported")
    ref = fake.plant(Library.BUSINESS, op.remote_path, b"no hash reported")
    fake._items[ref.item_id].quick_xor = None  # type: ignore[assignment]
    original = fake._meta

    def no_hash(item):
        m = original(item)
        return type(m)(m.ref, m.name, m.path, m.size, None, m.deleted, m.last_modified)

    fake._meta = no_hash  # type: ignore[method-assign]
    assert queue.process_one(provider=fake, gate=gate) == "adopted"
