"""M05.8: the monthly manifest: line format, month boundaries, concurrency, and the one allowed replace."""

import datetime
import json
import threading

import pytest
from ots.core.jobs import registered_jobs
from ots.storage import jobs as storage_jobs  # noqa: F401  (registers the jobs)
from ots.storage import manifest, queue, reconcile
from ots.storage.errors import WrongLibrary
from ots.storage.gate import QueueGate
from ots.storage.models import StorageOperation as Op
from ots.storage.provider import Library, assert_replaceable

from tests.storage.queue_helpers import make_op

pytestmark = pytest.mark.django_db
UTC = datetime.UTC


@pytest.fixture(autouse=True)
def _manifest_dir(settings, tmp_path):
    settings.OTS_MANIFEST_DIR = tmp_path / "manifests"


@pytest.fixture
def gate():
    return QueueGate(rng=lambda: 1.0)


def add(code="F-1", sha="a" * 64, size=10, item="item-1", at=None):
    return manifest.append(
        code, sha, size, "drive-1", item, at or datetime.datetime(2026, 11, 12, 6, 0, tzinfo=UTC)
    )


# --- the line format -------------------------------------------------------------------------------------


def test_a_line_has_exactly_the_planned_fields_and_is_one_json_object():
    assert add("C-2026-0042", "b" * 64, 1234, "item-9") is True
    text = manifest.manifest_path("2026-11").read_text()
    assert text.endswith("\n") and text.count("\n") == 1
    entry = json.loads(text)
    assert list(entry) == ["file_code", "sha256", "size", "drive_id", "item_id", "archived_at"]
    assert entry == {
        "file_code": "C-2026-0042", "sha256": "b" * 64, "size": 1234, "drive_id": "drive-1",
        "item_id": "item-9", "archived_at": "2026-11-12T11:30:00+05:30",
    }  # fmt: skip


def test_the_month_is_the_archive_date_in_india_not_utc():
    add("A", at=datetime.datetime(2026, 10, 31, 20, 0, tzinfo=UTC))  # 01:30 on 1 November in Kolkata
    add("B", at=datetime.datetime(2026, 11, 30, 17, 0, tzinfo=UTC))  # 22:30 on 30 November
    add("C", at=datetime.datetime(2026, 11, 30, 19, 0, tzinfo=UTC))  # 00:30 on 1 December
    assert manifest.manifest_path("2026-11").read_text().count("\n") == 2
    assert manifest.manifest_path("2026-12").read_text().count("\n") == 1


def test_the_same_file_is_not_listed_twice_but_a_new_version_is():
    assert add("F-1", item="item-1") and not add("F-1", item="item-1")  # a retry
    assert add("F-1", item="item-2")  # the repaired copy has a new item id: a second line
    assert manifest.manifest_path("2026-11").read_text().count("\n") == 2


def test_odd_characters_in_a_file_code_stay_valid_json():
    add('weird "name" \\ ü 日本', item="x")
    assert json.loads(manifest.manifest_path("2026-11").read_text())["file_code"] == 'weird "name" \\ ü 日本'


def test_two_workers_appending_at_once_never_corrupt_a_line():
    def worker(start):
        for i in range(start, start + 60):
            add(f"F-{i}", item=f"item-{i}")

    threads = [threading.Thread(target=worker, args=(s,)) for s in (0, 1000)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    lines = manifest.manifest_path("2026-11").read_text().splitlines()
    assert len(lines) == 120 and all(json.loads(line)["file_code"].startswith("F-") for line in lines)


# --- the one allowed replace -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path", ["Manifests/2026-11.jsonl", "/Manifests/2026-11.jsonl", "Manifests/1999-01.jsonl"]
)
def test_only_manifest_names_in_the_dr_library_are_replaceable(path):
    assert_replaceable(Library.DR, path)


@pytest.mark.parametrize(
    "library,path",
    [
        (Library.BUSINESS, "Manifests/2026-11.jsonl"),  # the wrong library
        (Library.DR, "Files/2026/11/F-1"),
        (Library.DR, "PostgreSQL/WAL/1/segment.zst.age"),
        (Library.DR, "Manifests/2026-11.jsonl.bak"),
        (Library.DR, "Manifests/../Files/x"),
        (Library.DR, "Manifests/sub/2026-11.jsonl"),
        (Library.DR, "manifests/2026-11.jsonl"),
        (Library.DR, "Manifests/26-11.jsonl"),
    ],
)
def test_everything_else_is_refused(library, path):
    with pytest.raises(WrongLibrary):
        assert_replaceable(library, path)


def test_the_fake_provider_enforces_the_allowlist_too(fake, tmp_path):
    p = tmp_path / "x"
    p.write_bytes(b"data")
    import hashlib

    with pytest.raises(WrongLibrary):
        fake.replace_dr("Files/2026/11/F-1", p, 4, hashlib.sha256(b"data").hexdigest())


def test_an_operation_that_asks_to_replace_a_normal_file_is_a_dead_letter_and_nothing_is_overwritten(
    fake, gate, tmp_path
):
    keep = make_op(tmp_path, b"precious original", name="keep")
    assert queue.process_one(provider=fake, gate=gate) == "done"
    evil = make_op(tmp_path, b"overwrite attempt", name="evil", remote=keep.remote_path)
    Op.objects.filter(pk=evil.pk).update(replace_existing=True)
    assert queue.process_one(provider=fake, gate=gate) == "failed"
    assert Op.objects.get(pk=evil.pk).state == Op.State.FAILED
    assert (
        b"".join(fake.open_read(fake.find_by_path(Library.BUSINESS, keep.remote_path).ref))
        == b"precious original"
    )


# --- the daily upload ------------------------------------------------------------------------------------------


def test_the_daily_job_uploads_a_changed_manifest_and_replaces_the_old_copy(fake, gate):
    add("F-1", item="i1")
    assert manifest.upload_changed() == 1
    op = Op.objects.get(ref_type="manifest")
    assert (op.kind, op.target_library, op.remote_path, op.replace_existing) == (
        Op.Kind.UPLOAD_BACKUP_OBJECT, "DR", "Manifests/2026-11.jsonl", True,
    )  # fmt: skip
    assert queue.process_one(provider=fake, gate=gate) == "done"
    first = b"".join(fake.open_read(fake.find_by_path(Library.DR, "Manifests/2026-11.jsonl").ref))
    assert first == manifest.manifest_path("2026-11").read_bytes()

    assert manifest.upload_changed() == 0  # nothing changed: nothing uploaded
    add("F-2", item="i2")
    assert manifest.upload_changed() == 1
    assert queue.process_one(provider=fake, gate=gate) == "done"
    second = b"".join(fake.open_read(fake.find_by_path(Library.DR, "Manifests/2026-11.jsonl").ref))
    assert second.count(b"\n") == 2 and len(fake.paths(Library.DR)) == 1  # replaced, not duplicated
    states = list(
        Op.objects.filter(ref_type="manifest").order_by("created_at").values_list("remote_state", flat=True)
    )
    assert states == ["SUPERSEDED", "OK"]


def test_reconciliation_does_not_raise_alarms_about_a_replaced_manifest(fake, gate):
    add("F-1", item="i1")
    manifest.upload_changed()
    queue.process_one(provider=fake, gate=gate)
    reconcile.run(provider=fake, gate=gate)
    add("F-2", item="i2")
    manifest.upload_changed()
    queue.process_one(provider=fake, gate=gate)
    stats = reconcile.run(provider=fake, gate=gate)
    assert not stats.get("missing") and not stats.get("content_changed")


def test_a_line_added_while_an_upload_is_queued_does_not_break_that_upload(fake, gate):
    add("F-1", item="i1")
    manifest.upload_changed()  # snapshot taken
    add("F-2", item="i2")  # the live manifest changes before the upload runs
    assert (
        queue.process_one(provider=fake, gate=gate) == "done"
    )  # the snapshot still matches its recorded hash
    assert manifest.upload_changed() == 1  # and the newer lines go up next time


def test_an_unfinished_upload_is_not_queued_twice(fake, gate):
    add("F-1", item="i1")
    assert manifest.upload_changed() == 1
    add("F-2", item="i2")
    assert (
        manifest.upload_changed() == 0
    )  # the first one is still waiting; tomorrow's run takes the newer lines
    assert Op.objects.filter(ref_type="manifest").count() == 1


def test_old_snapshots_are_cleaned_up_once_uploaded(fake, gate):
    add("F-1", item="i1")
    manifest.upload_changed()
    queue.process_one(provider=fake, gate=gate)
    manifest.upload_changed()  # nothing new, but housekeeping runs
    assert not list((manifest.manifest_dir() / "upload").glob("*.jsonl"))


def test_an_empty_manifest_is_not_uploaded():
    manifest.manifest_path("2026-11").write_text("")
    assert manifest.upload_changed() == 0


def test_the_daily_job_is_registered():
    assert registered_jobs()["storage.manifest_upload"].schedule == "30 3 * * *"


def test_the_real_graph_provider_also_refuses_before_sending_anything(tmp_path):
    import hashlib

    import httpx
    import respx

    from tests.storage.graph_helpers import make_provider

    p = tmp_path / "x"
    p.write_bytes(b"data")
    provider, _, _ = make_provider()
    with respx.mock(assert_all_called=False) as router:
        with pytest.raises(WrongLibrary):
            provider.replace_dr("Files/2026/11/F-1", p, 4, hashlib.sha256(b"data").hexdigest())
        assert len(router.calls) == 0 and isinstance(httpx.Client(), httpx.Client)
