"""F-FL-5: someone deletes an archived file in SharePoint. The next reconciliation marks it MISSING, raises a
CRITICAL alert, and re-uploads it from the local cache; afterwards SharePoint holds the file again."""

import pytest
from ots.core import alerts
from ots.storage import classify as c
from ots.storage import queue, reconcile
from ots.storage.gate import QueueGate
from ots.storage.handlers import clear_handlers, register_completion_handler
from ots.storage.models import StorageOperation as Op
from ots.storage.provider import Library, RemoteRef

from tests.storage.queue_helpers import make_op

pytestmark = pytest.mark.django_db


def test_delete_in_sharepoint_then_next_run_marks_missing_alerts_and_repairs(fake, tmp_path):
    clear_handlers()
    archived = []
    register_completion_handler(
        Op.Kind.ARCHIVE_FILE, lambda op, ref: archived.append(("archive", ref.item_id))
    )
    register_completion_handler(Op.Kind.REPAIR_FILE, lambda op, ref: archived.append(("repair", ref.item_id)))
    gate = QueueGate(rng=lambda: 1.0)

    op = make_op(tmp_path, b"the signed contract", name="C-2026-0042", ref_id="blob-42")
    assert queue.process_one(provider=fake, gate=gate) == "done"
    op.refresh_from_db()
    assert reconcile.run(provider=fake, gate=gate) == {}  # night 1: all is well

    fake.remote_delete(
        RemoteRef(Library.BUSINESS, op.drive_id, op.item_id)
    )  # a person deletes it in the web UI
    assert fake.find_by_path(Library.BUSINESS, op.remote_path) is None

    assert reconcile.run(provider=fake, gate=gate)["missing"] == 1  # night 2
    op.refresh_from_db()
    assert op.remote_state == Op.RemoteState.MISSING
    assert alerts.open_alerts().get(key=c.ALERT_MISSING).severity == "CRITICAL"

    assert queue.process_one(provider=fake, gate=gate) == "done"  # the repair upload
    back = fake.find_by_path(Library.BUSINESS, op.remote_path)
    assert back is not None and b"".join(fake.open_read(back.ref)) == b"the signed contract"
    assert [kind for kind, _ in archived] == ["archive", "repair"] and archived[1][1] == back.ref.item_id
    repaired = Op.objects.get(kind=Op.Kind.REPAIR_FILE)
    assert repaired.state == Op.State.DONE and repaired.remote_state == Op.RemoteState.OK

    assert reconcile.run(provider=fake, gate=gate) == {}  # night 3: the archive is whole again
    clear_handlers()
