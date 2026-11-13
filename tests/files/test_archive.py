"""M08.4: what the storage queue's completion does to files (section 3), and F-FL-4 / F-FL-5 through the real handlers."""

import datetime
import json
import uuid

import pytest
from ots.core import alerts
from ots.core.clock import now
from ots.core.models import OutboxEvent
from ots.files import handlers
from ots.files.models import Blob, FileState, FileVersion
from ots.storage import classify as c
from ots.storage import manifest, reconcile
from ots.storage.gate import QueueGate
from ots.storage.models import StorageOperation as Op
from ots.storage.provider import Library, RemoteRef

from tests.files.conftest import body, drain, login, upload_all

pytestmark = pytest.mark.django_db
DATA = bytes(range(256)) * 12


def manifest_lines(settings):
    out = []
    for f in sorted(settings.OTS_MANIFEST_DIR.glob("manifest-*.jsonl")):
        out += [json.loads(line) for line in f.read_text().splitlines()]
    return out


def test_an_archived_upload_turns_off_site_everywhere_at_once(office, remote, settings):
    upload_all(login("emp"), DATA, uuid.uuid4(), office["task"], name="Sales.xlsx")
    assert drain(remote) == 1
    blob, v, op = Blob.objects.get(), FileVersion.objects.get(), Op.objects.get()
    assert blob.state == v.state == FileState.ARCHIVED and op.state == Op.State.DONE
    assert blob.item_id == op.item_id and blob.drive_id and blob.archived_at and blob.ms_quick_xor_hash
    meta = remote.find_by_path(Library.BUSINESS, op.remote_path)
    assert meta is not None and meta.size == len(DATA) and op.remote_path.endswith(v.file_code)
    assert [m["file_code"] for m in manifest_lines(settings)] == [v.file_code]
    assert OutboxEvent.objects.filter(type="file.archived", aggregate_id=str(v.pk)).count() == 1
    page = login("sub").get(f"/app/tasks/{office['task'].pk}/files").content.decode()
    assert "Not yet off-site" not in page and "Off-site" in page


def test_two_versions_of_the_same_content_become_archived_together(office, remote, settings):
    c_ = login("emp")
    upload_all(c_, DATA, uuid.uuid4(), office["task"], name="a.xlsx")
    upload_all(c_, DATA, uuid.uuid4(), office["task"], name="b.xlsx")
    assert Op.objects.count() == 1
    drain(remote)
    assert set(FileVersion.objects.values_list("state", flat=True)) == {FileState.ARCHIVED}
    assert len(manifest_lines(settings)) == 2
    assert OutboxEvent.objects.filter(type="file.archived").count() == 2


def test_finishing_the_same_operation_twice_changes_nothing_more(office, remote, settings):
    upload_all(login("emp"), DATA, uuid.uuid4(), office["task"])
    drain(remote)
    op = Op.objects.get()
    handlers.on_archived(op, RemoteRef(Library.BUSINESS, op.drive_id, op.item_id, op.etag))
    assert len(manifest_lines(settings)) == 1
    assert OutboxEvent.objects.filter(type="file.archived").count() == 1


def test_a_failing_handler_leaves_the_file_not_off_site(office, remote, monkeypatch):
    upload_all(login("emp"), DATA, uuid.uuid4(), office["task"])
    monkeypatch.setattr(manifest, "append", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    drain(remote)
    assert FileVersion.objects.get().state == FileState.STAGED  # never claimed off-site
    assert Op.objects.get().state != Op.State.DONE
    monkeypatch.undo()
    assert Blob.objects.get().state == FileState.STAGED


def test_f_fl_5_a_file_deleted_in_sharepoint_is_marked_missing_then_repaired(office, remote):
    upload_all(login("emp"), DATA, uuid.uuid4(), office["task"], name="Contract.pdf")
    drain(remote)
    op = Op.objects.get(kind=Op.Kind.ARCHIVE_FILE)
    remote.remote_delete(RemoteRef(Library.BUSINESS, op.drive_id, op.item_id))
    gate = QueueGate(rng=lambda: 1.0)
    assert reconcile.run(provider=remote, gate=gate)["missing"] == 1
    assert (
        Blob.objects.get().state == FileState.MISSING and FileVersion.objects.get().state == FileState.MISSING
    )
    assert alerts.open_alerts().get(key=c.ALERT_MISSING).severity == "CRITICAL"
    page = login("sub").get(f"/app/tasks/{office['task'].pk}/files").content.decode()
    assert "Off-site problem" in page
    drain(remote)  # the repair upload, from the cached copy
    assert (
        Blob.objects.get().state == FileState.ARCHIVED
        and FileVersion.objects.get().state == FileState.ARCHIVED
    )
    back = remote.find_by_path(Library.BUSINESS, op.remote_path)
    assert back is not None and b"".join(remote.open_read(back.ref)) == DATA
    assert Blob.objects.get().item_id == back.ref.item_id  # the blob now points at the new item
    assert OutboxEvent.objects.filter(type="file.repaired").count() == 1
    assert reconcile.run(provider=remote, gate=gate) == {}


def test_a_drifted_file_is_marked_drifted(office, remote):
    upload_all(login("emp"), DATA, uuid.uuid4(), office["task"])
    drain(remote)
    op = Op.objects.get()
    meta = remote.find_by_path(Library.BUSINESS, op.remote_path)
    handlers.on_drifted(op, meta)
    assert FileVersion.objects.get().state == FileState.DRIFTED
    handlers.on_drifted(op, meta)  # again: no second event
    assert OutboxEvent.objects.filter(type="file.drifted").count() == 1


def test_the_cache_keeps_a_missing_files_only_copy(office, remote):
    from ots.files import cache

    upload_all(login("emp"), DATA, uuid.uuid4(), office["task"])
    drain(remote)
    op = Op.objects.get()
    Blob.objects.update(state=FileState.MISSING)
    assert cache.evict(0) == 0 and Blob.objects.get().cached
    assert body(login("sub").get(f"/files/v/{FileVersion.objects.get().pk}/download")) == DATA
    assert op.state == Op.State.DONE


def test_f_fl_4_an_upload_whose_commit_was_lost_is_adopted_by_the_sweep(office, remote):
    """The bytes reached SharePoint, but the process died before the database recorded it. The next sweep finds
    the item, adopts it, and the version reaches ARCHIVED with exactly one copy in SharePoint."""
    from ots.storage import sweep

    upload_all(login("emp"), DATA, uuid.uuid4(), office["task"])
    op = Op.objects.get()
    ref = remote.put(
        Library.BUSINESS,
        op.remote_path,
        __import__("pathlib").Path(op.local_path),
        op.size_bytes,
        op.sha256,
        _Mem(),
    )
    assert Op.objects.get().state == Op.State.PENDING and FileVersion.objects.get().state == FileState.STAGED
    later = now() + datetime.timedelta(minutes=20)  # the sweep ignores items younger than its grace period
    sweep.run(provider=remote, gate=QueueGate(rng=lambda: 1.0), clock=lambda: later)
    assert FileVersion.objects.get().state == FileState.ARCHIVED
    assert Blob.objects.get().item_id == ref.item_id
    copies = [m for m in remote.changes(Library.BUSINESS, None)[0] if m.path == op.remote_path]
    assert len(copies) == 1


class _Mem:
    id = "mem"

    def save_session(self, *_): ...
    def save_progress(self, *_): ...
    def clear_session(self): ...
