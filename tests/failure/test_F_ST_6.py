"""F-ST-6: the upload session expired mid-upload (404 on the uploadUrl). If the file is already complete in
SharePoint it is adopted; otherwise the session is cleared and the upload starts again from byte 0."""

import pytest
from ots.storage import queue
from ots.storage.errors import GraphError
from ots.storage.gate import QueueGate
from ots.storage.models import StorageOperation as Op
from ots.storage.provider import Library

from tests.storage.queue_helpers import make_op

pytestmark = pytest.mark.django_db


def expired():
    return GraphError(404, code="itemNotFound", url_kind="upload")


def test_session_gone_but_the_file_is_already_there_is_adopted(fake, tmp_path):
    gate = QueueGate(rng=lambda: 1.0)
    op = make_op(tmp_path, b"all of it")
    op.save_session("https://u.example/old", None)
    op.save_progress(5_242_880)
    fake.plant(
        Library.BUSINESS, op.remote_path, b"all of it"
    )  # the final piece landed; we never got the answer
    fake.inject([expired()])
    assert queue.process_one(provider=fake, gate=gate) == "adopted"
    op.refresh_from_db()
    assert op.state == Op.State.DONE and op.item_id
    assert len(fake.paths(Library.BUSINESS)) == 1  # nothing was uploaded twice


def test_session_gone_and_the_file_is_not_there_restarts_from_zero(fake, tmp_path):
    gate = QueueGate(rng=lambda: 1.0)
    op = make_op(tmp_path, b"half of it never arrived")
    op.save_session("https://u.example/old", None)
    op.save_progress(5_242_880)
    fake.inject([expired()])
    assert queue.process_one(provider=fake, gate=gate) == "restarted"
    op.refresh_from_db()
    assert (op.state, op.upload_url, op.confirmed_bytes, op.attempts) == (Op.State.PENDING, "", 0, 1)
    assert queue.process_one(provider=fake, gate=gate) == "done"  # the fresh attempt succeeds
    op.refresh_from_db()
    assert op.state == Op.State.DONE


def test_a_partial_remote_file_is_not_mistaken_for_ours(fake, tmp_path):
    gate = QueueGate(rng=lambda: 1.0)
    op = make_op(tmp_path, b"the complete content")
    fake.plant(Library.BUSINESS, op.remote_path, b"the compl")  # truncated: different size
    fake.inject([expired()])
    assert queue.process_one(provider=fake, gate=gate) == "restarted"  # not adopted


def test_a_session_that_keeps_expiring_eventually_dead_letters(fake, tmp_path):
    gate = QueueGate(rng=lambda: 1.0)
    op = make_op(tmp_path, b"unlucky")
    fake.inject([expired(), None] * 10)  # the upload fails, the look-up that follows it works
    for _ in range(10):
        queue.process_one(provider=fake, gate=gate)
    op.refresh_from_db()
    assert op.state == Op.State.FAILED and "keeps expiring" in op.last_error
