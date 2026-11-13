"""F-FL-6: an archived file is wanted while SharePoint cannot be reached and the cache no longer holds it.
The person gets a clear page instead of an error, is remembered, and the worker fetches the file as soon as it
can and notifies them. A file that is still in the cache never needs the internet."""

import datetime

import pytest
from ots.audit.models import AuditLog
from ots.core import alerts
from ots.core.clock import now
from ots.files import cache, fetch
from ots.files.models import Blob, FileFetchRequest, FileVersion
from ots.notifications.models import Kind, Notification
from ots.storage import classify as c
from ots.storage.errors import TransportFailure
from ots.storage.gate import QueueGate

from tests.files.conftest import body, drain, login, upload_all

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("dirs", "file_handlers")]
DATA = bytes(range(256)) * 20


@pytest.fixture
def archived(office, remote):
    import uuid

    upload_all(login("emp"), DATA, uuid.uuid4(), office["task"], name="Contract.pdf")
    drain(remote)
    v = FileVersion.objects.get()
    cache.evict(0)  # the cache no longer holds it
    return v


def offline(monkeypatch, remote):
    def boom(ref):
        raise TransportFailure("no route to host")
        yield b""  # pragma: no cover  (makes this a generator, like the real open_read)

    monkeypatch.setattr(remote, "open_read", boom)


def test_cached_files_never_need_the_internet(office, remote, monkeypatch):
    import uuid

    upload_all(login("emp"), DATA, uuid.uuid4(), office["task"])
    drain(remote)
    offline(monkeypatch, remote)
    r = login("sub").get(f"/files/v/{FileVersion.objects.get().pk}/download")
    assert r.status_code == 200 and body(r) == DATA


def test_unreachable_sharepoint_gives_the_waiting_page_and_remembers_the_person(
    archived, office, remote, monkeypatch
):
    offline(monkeypatch, remote)
    r = login("sub").get(f"/files/v/{archived.pk}/download")
    text = r.content.decode()
    assert r.status_code == 503 and "available when the internet returns" in text and "Contract.pdf" in text
    assert r["Retry-After"] == "300"
    req = FileFetchRequest.objects.get()
    assert (req.version, req.user, req.state) == (archived, office["sub"], "PENDING")
    assert AuditLog.objects.filter(action="file.fetch_requested").count() == 1
    login("sub").get(f"/files/v/{archived.pk}/download")  # asking again changes nothing
    assert (
        FileFetchRequest.objects.count() == 1
        and AuditLog.objects.filter(action="file.fetch_requested").count() == 1
    )
    assert not __import__("ots.files.models", fromlist=["FileAccessLog"]).FileAccessLog.objects.exists()


def test_http_errors_from_sharepoint_count_as_unreachable_too(archived, office, remote):
    for status in (500, 503, 429, 401):
        FileFetchRequest.objects.all().delete()
        remote.inject([status])
        assert login("sub").get(f"/files/v/{archived.pk}/download").status_code == 503
        assert FileFetchRequest.objects.count() == 1, status


def test_a_blocked_queue_does_not_even_try_sharepoint(archived, remote, monkeypatch):
    alerts.raise_alert("storage.ops_block", "CRITICAL", "BLOCKED_AUTH", '{"scope": "ALL", "reason": "cert"}')
    called = []
    monkeypatch.setattr(remote, "open_read", lambda ref: called.append(1))
    QueueGate()  # the gate reads the alert
    r = login("sub").get(f"/files/v/{archived.pk}/download")
    assert r.status_code == 503 and called == [] and FileFetchRequest.objects.count() == 1


def test_the_worker_waits_while_it_is_still_down_then_caches_the_file_and_notifies(
    archived, office, remote, monkeypatch
):
    offline(monkeypatch, remote)
    login("sub").get(f"/files/v/{archived.pk}/download")
    assert fetch.run() == {
        "expired": 0,
        "fetched": 0,
        "waiting": 1,
    }  # still down: nothing happens, nothing is lost
    assert FileFetchRequest.objects.get().state == "PENDING" and not Notification.objects.exists()
    monkeypatch.undo()  # the internet is back
    assert fetch.run()["fetched"] == 1
    req = FileFetchRequest.objects.get()
    assert req.state == "DONE" and req.done_at is not None
    n = Notification.objects.get()
    assert (n.user, n.kind, n.task) == (
        office["sub"],
        Kind.FILE_READY,
        office["task"],
    ) and "Contract.pdf" in n.title
    assert Blob.objects.get().cached
    r = login("sub").get(f"/files/v/{archived.pk}/download")  # and now it is just a cache hit
    assert r.status_code == 200 and body(r) == DATA
    assert (
        __import__("ots.files.models", fromlist=["FileAccessLog"]).FileAccessLog.objects.get().source
        == "CACHE"
    )


def test_everyone_who_asked_is_notified_once(archived, office, remote, monkeypatch):
    offline(monkeypatch, remote)
    for who in ("sub", "emp", "boss"):
        login(who).get(f"/files/v/{archived.pk}/download")
    monkeypatch.undo()
    fetch.run()
    assert Notification.objects.count() == 3 and set(
        Notification.objects.values_list("user__login_id", flat=True)
    ) == {"sub", "emp", "boss"}
    fetch.run()
    assert Notification.objects.count() == 3  # a second run finds nothing to do


def test_a_request_nobody_could_fill_in_three_days_expires(archived, office, remote, monkeypatch):
    offline(monkeypatch, remote)
    login("sub").get(f"/files/v/{archived.pk}/download")
    FileFetchRequest.objects.update(created_at=now() - datetime.timedelta(days=4))
    assert fetch.run()["expired"] == 1 and FileFetchRequest.objects.get().state == "EXPIRED"
    login("sub").get(f"/files/v/{archived.pk}/download")  # asking again starts a fresh request
    assert FileFetchRequest.objects.get().state == "PENDING"


def test_a_corrupt_fetch_is_not_cached_and_nobody_is_told_it_is_ready(archived, office, remote, monkeypatch):
    offline(monkeypatch, remote)
    login("sub").get(f"/files/v/{archived.pk}/download")
    monkeypatch.undo()
    remote._items[Blob.objects.get().item_id].file.write_bytes(b"\x00" * len(DATA))
    assert fetch.run()["fetched"] == 0
    assert not Blob.objects.get().cached and not Notification.objects.exists()
    assert FileFetchRequest.objects.get().state == "PENDING"


def test_a_library_file_with_no_task_is_fetched_without_a_bell_entry(office, remote, monkeypatch):
    import uuid

    upload_all(login("boss"), DATA, uuid.uuid4())  # no task: the library
    drain(remote)
    v = FileVersion.objects.get()
    cache.evict(0)
    offline(monkeypatch, remote)
    assert login("emp").get(f"/files/v/{v.pk}/download").status_code == 503
    monkeypatch.undo()
    assert fetch.run()["fetched"] == 1
    assert FileFetchRequest.objects.get().state == "DONE" and not Notification.objects.exists()


def test_the_job_is_registered_every_minute():
    import ots.files.jobs  # noqa: F401
    from ots.core.jobs import registered_jobs

    assert registered_jobs()["files.fetch_pending"].schedule == "* * * * *"
    assert c.ALERT_OFFLINE
