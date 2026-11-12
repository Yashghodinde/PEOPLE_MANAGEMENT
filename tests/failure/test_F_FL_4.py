"""F-FL-4: orphan remote file. The upload succeeded in SharePoint, but the database commit failed (the process
died in between). The next sweep adopts the item: the version reaches ARCHIVED, and SharePoint holds exactly one
copy (no duplicate)."""

import datetime

import pytest
from freezegun import freeze_time
from ots.storage import queue, sweep
from ots.storage.gate import QueueGate
from ots.storage.handlers import clear_handlers, register_completion_handler
from ots.storage.models import StorageOperation as Op
from ots.storage.provider import Library

from tests.storage.queue_helpers import make_op

pytestmark = pytest.mark.django_db


def test_upload_succeeds_then_the_commit_fails_and_the_next_sweep_adopts_it(fake, tmp_path):
    clear_handlers()
    archived_versions = []
    register_completion_handler(Op.Kind.ARCHIVE_FILE, lambda op, ref: archived_versions.append(op.ref_id))
    gate = QueueGate(rng=lambda: 1.0)
    op = make_op(tmp_path, b"quarterly report", name="R-2026-Q3", ref_id="version-77")

    # the worker uploads the file, then dies before it can record the result
    claimed = queue.claim([Op.Kind.ARCHIVE_FILE])
    assert claimed.pk == op.pk
    fake.put(Library.BUSINESS, op.remote_path, tmp_path / "R-2026-Q3.bin", op.size_bytes, op.sha256, claimed)
    assert Op.objects.get(pk=op.pk).state == Op.State.RUNNING and archived_versions == []

    with freeze_time("2030-01-01 06:00:00") as clock:
        assert queue.reset_stale_running(older_than=datetime.timedelta(0)) == 1  # the worker restarts
        clock.tick(datetime.timedelta(minutes=20))
        stats = sweep.run(provider=fake, gate=gate)  # the 02:30 sweep comes before the retry's backoff ends

    done = Op.objects.get(pk=op.pk)
    assert stats["adopted"] == 1 and done.state == Op.State.DONE and done.item_id
    assert archived_versions == ["version-77"]  # the version reached ARCHIVED
    assert len(fake.paths(Library.BUSINESS)) == 1  # no duplicate remote item
    assert fake.find_by_path(Library.BUSINESS, op.remote_path).ref.item_id == done.item_id  # type: ignore[union-attr]
    assert queue.process_one(provider=fake, gate=gate) == "idle"  # nothing is left to upload
    clear_handlers()
