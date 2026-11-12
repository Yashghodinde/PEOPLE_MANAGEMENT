"""Two workers at once: SKIP LOCKED means no operation is ever processed twice."""

import threading

import pytest
from django.db import connection
from ots.storage import queue
from ots.storage.fake import LocalDirProvider
from ots.storage.gate import QueueGate
from ots.storage.models import StorageOperation as Op

from tests.storage.queue_helpers import make_op


class CountingProvider(LocalDirProvider):
    """Records every put by operation id; a double-processed operation would appear twice."""

    def __init__(self, root):
        super().__init__(root)
        self.put_ops: list[str] = []
        self._lock = threading.Lock()

    def put(self, library, remote_path, local_path, size, sha256, op):
        with self._lock:
            self.put_ops.append(str(op.id))
        threading.Event().wait(0.01)  # a moment of "uploading" so the two workers overlap
        return super().put(library, remote_path, local_path, size, sha256, op)


@pytest.fixture
def keep_default_company():
    yield
    from ots.accounts.models import Company

    Company.objects.get_or_create(code="DEFAULT", defaults={"name": "Default company"})


@pytest.mark.django_db(transaction=True)
def test_two_workers_never_process_the_same_operation(tmp_path, keep_default_company):
    provider = CountingProvider(tmp_path / "remote")
    ids = [make_op(tmp_path, f"data-{i}".encode(), name=f"f{i}").pk for i in range(40)]
    errors: list[BaseException] = []

    def worker():
        try:
            gate = QueueGate(rng=lambda: 1.0)
            while queue.process_one(provider=provider, gate=gate) != "idle":
                pass
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not errors, errors
    assert sorted(provider.put_ops) == sorted(str(i) for i in ids)  # each exactly once
    assert Op.objects.filter(state=Op.State.DONE).count() == 40
    assert (
        len(provider.paths(__import__("ots.storage.provider", fromlist=["Library"]).Library.BUSINESS)) == 40
    )


@pytest.mark.django_db(transaction=True)
def test_a_worker_skips_a_row_another_worker_holds_instead_of_waiting(tmp_path, keep_default_company):
    """SKIP LOCKED: while worker A holds the oldest operation, worker B takes the next one at once."""
    from django.db import transaction

    first = make_op(tmp_path, b"one", name="first")
    second = make_op(tmp_path, b"two", name="second")
    holding, release = threading.Event(), threading.Event()
    result: dict[str, object] = {}

    def holder():  # worker A: has claimed `first` and is still inside its transaction
        try:
            with transaction.atomic():
                Op.objects.select_for_update().get(pk=first.pk)
                holding.set()
                release.wait(15)
        finally:
            connection.close()

    def claimer():  # worker B
        try:
            op = queue.claim([k for k in Op.Kind.values])
            result["pk"] = op.pk if op else None
        finally:
            connection.close()

    a = threading.Thread(target=holder)
    a.start()
    assert holding.wait(5)
    b = threading.Thread(target=claimer)
    b.start()
    b.join(5)  # without SKIP LOCKED this blocks until A lets go, and the join times out
    still_waiting = b.is_alive()
    release.set()
    a.join(10)
    b.join(10)
    assert not still_waiting, "worker B waited behind worker A's lock"
    assert result["pk"] == second.pk
