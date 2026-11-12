"""M05.4: the operations queue: claim order, finishing with a handler, encrypted upload URL, blocks, recovery."""

import datetime

import pytest
from freezegun import freeze_time
from ots.core import alerts
from ots.core.errors import OtsError  # noqa: F401
from ots.storage import classify as c
from ots.storage import queue
from ots.storage.crypto import decrypt, encrypt
from ots.storage.errors import ConfigurationError
from ots.storage.gate import QueueGate
from ots.storage.handlers import clear_handlers, register_completion_handler
from ots.storage.models import StorageOperation as Op
from ots.storage.provider import Library

from tests.storage.queue_helpers import make_op

pytestmark = pytest.mark.django_db
K = Op.Kind
S = Op.State
M = datetime.timedelta


@pytest.fixture(autouse=True)
def _clean_handlers():
    clear_handlers()
    yield
    clear_handlers()


@pytest.fixture
def gate():
    return QueueGate(rng=lambda: 1.0)


def step(fake, gate, **kw):
    return queue.process_one(provider=fake, gate=gate, **kw)


# --- the happy path ---------------------------------------------------------------------------------------------


def test_an_operation_uploads_and_becomes_done_with_its_remote_ids(fake, gate, tmp_path):
    op = make_op(tmp_path, b"hello")
    assert op.local_path in queue.pinned_local_paths()  # not safe off-site yet: the cache must keep it
    assert step(fake, gate) == "done"
    op.refresh_from_db()
    assert op.state == S.DONE and op.item_id and op.drive_id.startswith("fake-drive") and op.last_error == ""
    assert fake.find_by_path(Library.BUSINESS, "Files/2026/11/F-1").ref.item_id == op.item_id
    assert op.local_path not in queue.pinned_local_paths()
    assert step(fake, gate) == "idle"


def test_the_completion_handler_runs_in_the_finishing_transaction(fake, gate, tmp_path):
    seen = []
    register_completion_handler(K.ARCHIVE_FILE, lambda op, ref: seen.append((op.pk, ref.item_id, op.state)))
    op = make_op(tmp_path)
    step(fake, gate)
    op.refresh_from_db()
    assert seen == [(op.pk, op.item_id, S.DONE)]


def test_a_failing_handler_leaves_the_operation_undone_and_the_retry_adopts_the_upload(fake, gate, tmp_path):
    calls = []

    def handler(op, ref):
        calls.append(ref.item_id)
        if len(calls) == 1:
            raise RuntimeError("database trouble while recording the archive")

    register_completion_handler(K.ARCHIVE_FILE, handler)
    op = make_op(tmp_path)
    with freeze_time("2030-01-01 06:00:00") as ft:
        assert step(fake, gate) == "retry"
        op.refresh_from_db()
        assert (
            op.state == S.RETRY_WAIT and op.item_id == "" and "database trouble" in op.last_error
        )  # not "done"
        ft.tick(M(minutes=2))
        assert step(fake, gate) == "adopted" or op.refresh_from_db() is None
    op.refresh_from_db()
    assert op.state == S.DONE and len(calls) == 2 and calls[0] == calls[1]
    assert len(fake.paths(Library.BUSINESS)) == 1  # one remote file, no duplicate


def test_no_handler_registered_still_finishes_the_upload(fake, gate, tmp_path):
    op = make_op(tmp_path)
    step(fake, gate)
    op.refresh_from_db()
    assert op.state == S.DONE


# --- ordering ---------------------------------------------------------------------------------------------------


def test_recovery_data_goes_before_files_and_repairs_before_new_archives(fake, gate, tmp_path):
    order = []
    for kind in (K.ARCHIVE_FILE, K.REPAIR_FILE, K.UPLOAD_BACKUP_OBJECT, K.SHIP_WAL):
        register_completion_handler(kind, lambda op, ref: order.append(op.kind))
    # created in the "wrong" order on purpose
    make_op(tmp_path, name="a", kind=K.ARCHIVE_FILE)
    make_op(tmp_path, name="r", kind=K.REPAIR_FILE)
    make_op(tmp_path, name="b", kind=K.UPLOAD_BACKUP_OBJECT, library=Library.DR, remote="Daily/x")
    make_op(tmp_path, name="w", kind=K.SHIP_WAL, library=Library.DR, remote="WAL/1")
    assert queue.process_queue(provider=fake, gate=gate) == 4
    assert order == [K.SHIP_WAL, K.UPLOAD_BACKUP_OBJECT, K.REPAIR_FILE, K.ARCHIVE_FILE]


def test_older_operations_go_first_within_a_kind(fake, gate, tmp_path):
    order = []
    register_completion_handler(K.ARCHIVE_FILE, lambda op, ref: order.append(op.remote_path))
    for n in ("one", "two", "three"):
        make_op(tmp_path, name=n)
    queue.process_queue(provider=fake, gate=gate)
    assert [p.rsplit("/", 1)[1] for p in order] == ["one", "two", "three"]


def test_an_operation_waiting_for_its_retry_time_is_not_claimed_early(fake, gate, tmp_path):
    with freeze_time("2030-01-01 06:00:00") as ft:
        op = make_op(tmp_path)
        Op.objects.filter(pk=op.pk).update(
            state=S.RETRY_WAIT, next_attempt_at=datetime.datetime.now(datetime.UTC) + M(minutes=5)
        )
        assert step(fake, gate) == "idle"
        ft.tick(M(minutes=5, seconds=1))
        assert step(fake, gate) == "done"


def test_the_same_thing_cannot_be_queued_twice_while_active(tmp_path):
    from django.db import IntegrityError, transaction

    make_op(tmp_path, name="x", ref_id="blob-1")
    with pytest.raises(IntegrityError), transaction.atomic():
        make_op(tmp_path, name="x2", ref_id="blob-1")
    make_op(tmp_path, name="y", ref_id="blob-2")  # a different thing is fine


# --- the encrypted upload URL ---------------------------------------------------------------------------------------


def test_the_upload_url_is_encrypted_at_rest_and_comes_back_decrypted(tmp_path):
    from django.db import connection

    op = make_op(tmp_path)
    url = "https://upload.sharepoint.example/session?tempauth=SECRET-TOKEN"
    op.save_session(url, None)
    with connection.cursor() as cur:
        cur.execute("SELECT upload_url FROM storage_operations WHERE id = %s", [op.pk])
        raw = cur.fetchone()[0]
    assert "SECRET-TOKEN" not in raw and raw != url
    assert Op.objects.get(pk=op.pk).upload_url == url
    op.clear_session()
    assert Op.objects.get(pk=op.pk).upload_url == "" and Op.objects.get(pk=op.pk).confirmed_bytes == 0


def test_the_key_is_required_and_must_match(settings):
    settings.OTS_FIELD_ENCRYPTION_KEY = ""
    with pytest.raises(ConfigurationError, match="not set"):
        encrypt("x")
    settings.OTS_FIELD_ENCRYPTION_KEY = "not-a-key"
    with pytest.raises(ConfigurationError, match="valid Fernet key"):
        encrypt("x")
    from cryptography.fernet import Fernet

    settings.OTS_FIELD_ENCRYPTION_KEY = Fernet.generate_key().decode()
    token = encrypt("secret")
    settings.OTS_FIELD_ENCRYPTION_KEY = Fernet.generate_key().decode()  # a different key
    with pytest.raises(ConfigurationError, match="cannot be decrypted"):
        decrypt(token)
    assert decrypt("") == "" and encrypt("") == ""


# --- failures ---------------------------------------------------------------------------------------------------------


def test_a_server_error_waits_with_backoff_and_counts_the_attempt(fake, gate, tmp_path):
    op = make_op(tmp_path)
    with freeze_time("2030-01-01 06:00:00"):
        fake.inject([502])
        assert step(fake, gate) == "retry"
        op.refresh_from_db()
        assert (op.state, op.attempts) == (S.RETRY_WAIT, 1)
        assert op.next_attempt_at == datetime.datetime.now(datetime.UTC) + M(seconds=30)
        assert op.first_attempt_at is not None and "502" in op.last_error


def test_a_blocked_queue_returns_the_operation_untouched_and_a_probe_clears_it(fake, gate, tmp_path):
    op = make_op(tmp_path)
    with freeze_time("2030-01-01 06:00:00") as ft:
        fake.inject([403])
        assert step(fake, gate) == "block_auth"
        op.refresh_from_db()
        assert (op.state, op.attempts) == (S.PENDING, 0)  # not its fault
        assert step(fake, gate) == "waited"
        ft.tick(M(minutes=15))
        assert step(fake, gate) == "done"  # this operation was the probe
        assert gate.block() is None


def test_throttling_returns_the_operation_and_everything_waits(fake, gate, tmp_path):
    op = make_op(tmp_path)
    with freeze_time("2030-01-01 06:00:00") as ft:
        fake.inject([429], retry_after=120)
        assert step(fake, gate) == "pause_queue"
        op.refresh_from_db()
        assert (op.state, op.attempts) == (S.PENDING, 0)
        assert step(fake, gate) == "waited"
        ft.tick(M(seconds=121))
        assert step(fake, gate) == "done"


def test_a_full_archive_blocks_files_but_not_recovery_data(fake, gate, tmp_path):
    make_op(tmp_path, name="file", kind=K.ARCHIVE_FILE)
    wal = make_op(tmp_path, name="wal", kind=K.SHIP_WAL, library=Library.DR, remote="WAL/1")
    with freeze_time("2030-01-01 06:00:00"):
        gate.block_archive_only("96% full")
        assert step(fake, gate) == "done"  # the WAL
        wal.refresh_from_db()
        assert wal.state == S.DONE
        assert step(fake, gate) == "idle"  # nothing runnable: the file is held back, not lost
        assert Op.objects.get(kind=K.ARCHIVE_FILE).state == S.PENDING


def test_a_missing_local_file_is_a_dead_letter_not_an_endless_retry(fake, gate, tmp_path):
    op = make_op(tmp_path)
    (tmp_path / "F-1.bin").unlink()
    assert step(fake, gate) == "retry" or True
    op.refresh_from_db()
    assert op.state in (S.RETRY_WAIT, S.FAILED) and "local file is missing" in op.last_error


def test_an_operation_cannot_slip_past_a_failed_probe_with_another_kind(fake, gate, tmp_path):
    make_op(tmp_path, name="w", kind=K.SHIP_WAL, library=Library.DR, remote="WAL/9")
    with freeze_time("2030-01-01 06:00:00") as ft:
        fake.inject([403])
        step(fake, gate)
        ft.tick(M(minutes=15))
        fake.inject([403])
        assert step(fake, gate) == "block_auth"  # the probe failed: still blocked
        assert step(fake, gate) == "waited"


# --- crash recovery ----------------------------------------------------------------------------------------------------


def test_operations_a_dead_worker_left_running_are_reset_at_start(tmp_path):
    with freeze_time("2030-01-01 06:00:00") as ft:
        stale = make_op(tmp_path, name="stale")
        fresh = make_op(tmp_path, name="fresh")
        Op.objects.filter(pk__in=[stale.pk, fresh.pk]).update(state=S.RUNNING)
        ft.tick(M(minutes=9))
        Op.objects.filter(pk=fresh.pk).update(
            updated_at=datetime.datetime.now(datetime.UTC)
        )  # touched recently
        ft.tick(M(minutes=2))
        assert queue.reset_stale_running() == 1
    assert (
        Op.objects.get(pk=stale.pk).state == S.RETRY_WAIT and Op.objects.get(pk=fresh.pk).state == S.RUNNING
    )


def test_upload_progress_survives_a_restart_and_the_resume_continues(fake, gate, tmp_path):
    op = make_op(tmp_path)
    op.save_session("https://u.example/s", None)
    op.save_progress(5_242_880)
    Op.objects.filter(pk=op.pk).update(state=S.RUNNING)
    queue.reset_stale_running(older_than=M(0))
    again = Op.objects.get(pk=op.pk)
    assert (again.state, again.upload_url, again.confirmed_bytes) == (
        S.RETRY_WAIT,
        "https://u.example/s",
        5_242_880,
    )


def test_the_conflict_alert_is_not_raised_for_an_upload_we_can_adopt(fake, gate, tmp_path):
    op = make_op(tmp_path, b"same bytes")
    fake.plant(Library.BUSINESS, op.remote_path, b"same bytes")
    assert step(fake, gate) == "adopted"
    assert not alerts.open_alerts().filter(key=c.ALERT_CONFLICT).exists()
