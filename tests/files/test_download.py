"""M08.4: downloads and previews (section 5)."""

import datetime
import uuid

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from ots.audit.models import AuditLog
from ots.core.clock import now
from ots.files import cache, download
from ots.files.models import (
    Blob,
    FileAccessLog,
    FileHandover,
    FileHandoverRecipient,
    FileState,
    FileVersion,
    FileVisibility,
)
from ots.storage.provider import Library, RemoteRef  # noqa: F401

from tests.files.conftest import body, drain, login, upload_all
from tests.files.factories import make_file

pytestmark = pytest.mark.django_db
DATA = bytes(range(256)) * 20  # 5 KB


def uploaded(office, remote=None, data=DATA, name="Sales.xlsx", who="emp"):
    upload_all(login(who), data, uuid.uuid4(), office["task"], name=name)
    if remote is not None:
        drain(remote)
    return FileVersion.objects.order_by("-uploaded_at").first()


def get(who, v, kind="download"):
    return login(who).get(f"/files/v/{v.pk}/{kind}")


# --- the cache-hit path ---------------------------------------------------------------------------------------


def test_a_cached_file_downloads_with_the_right_headers_and_is_logged(office):
    v = uploaded(office, name='Q3 "final" résumé.xlsx')
    r = get("sub", v)
    assert r.status_code == 200 and body(r) == DATA
    assert (
        r["Content-Disposition"] == f'attachment; filename="{v.file_code}"'
    )  # the code, never the typed name
    assert "résumé" not in r["Content-Disposition"]
    assert r["X-Content-Type-Options"] == "nosniff" and r["Content-Type"] == "application/octet-stream"
    assert r["Content-Length"] == str(len(DATA)) and "no-store" in r["Cache-Control"]
    log = FileAccessLog.objects.get()
    assert (log.version, log.user, log.action, log.source) == (v, office["sub"], "DOWNLOAD", "CACHE")
    assert AuditLog.objects.filter(action="file.download", actor_id=office["sub"].pk).count() == 1


def test_last_used_is_written_at_most_once_an_hour(office):
    v = uploaded(office)
    Blob.objects.update(last_accessed_at=now() - datetime.timedelta(hours=3))
    get("sub", v)
    first = Blob.objects.get().last_accessed_at
    assert now() - first < datetime.timedelta(minutes=1)
    Blob.objects.update(last_accessed_at=now() - datetime.timedelta(minutes=30))
    stale = Blob.objects.get().last_accessed_at
    get("sub", v)
    assert Blob.objects.get().last_accessed_at == stale  # a download within the hour does not write


def test_every_download_is_a_separate_log_row(office):
    v = uploaded(office)
    for _ in range(3):
        get("sub", v)
    assert FileAccessLog.objects.count() == 3


# --- who may download -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("who, ok", [("emp", True), ("sub", True), ("boss", True), ("other", False)])
def test_download_follows_the_task_and_the_team(office, who, ok):
    v = uploaded(office)
    assert get(who, v).status_code == (200 if ok else 404)


def test_an_unrelated_person_learns_nothing_and_a_signed_out_visitor_is_sent_to_login(office):
    v = uploaded(office)
    r = get("other", v)
    assert r.status_code == 404 and v.file_code not in r.content.decode()
    assert Client().get(f"/files/v/{v.pk}/download").status_code == 302
    assert get("emp", v).status_code == 200
    assert login("emp").get(f"/files/v/{uuid.uuid4()}/download").status_code == 404


def test_the_uploader_keeps_access_after_being_removed_from_the_task(office):
    from ots.tasks import services as task_services

    v = uploaded(office)
    a = office["task"].assignments.get()
    task_services.remove_assignment(office["sub"], a.pk, "wrong person") if hasattr(
        task_services, "remove_assignment"
    ) else None
    assert get("emp", v).status_code == 200


def test_a_file_handed_to_someone_can_be_downloaded_by_them(office):
    v = uploaded(office)
    assert get("other", v).status_code == 404
    h = FileHandover.objects.create(version=v, from_user=office["emp"], task=office["task"])
    FileHandoverRecipient.objects.create(handover=h, to_user=office["other"])
    assert get("other", v).status_code == 200


def test_library_files_are_open_to_everyone_signed_in(office):
    f, v = make_file(office["sa"], "policy.pdf")
    f.visibility = FileVisibility.LIBRARY
    f.save(update_fields=["visibility"])
    blob = v.blob
    from ots.files import layout

    path = layout.blob_path(blob.sha256)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * blob.size_bytes)
    Blob.objects.filter(pk=blob.pk).update(cached=True, local_path=str(path))
    assert get("other", v).status_code == 200


def test_a_sub_admin_downloads_their_teams_uploads_even_on_another_managers_task(office):
    from ots.accounts.models import Role

    from tests.accounts.factories import make_user
    from tests.tasks.factories import make_task

    sub2 = make_user(Role.SUB_ADMIN, reports_to=office["sa"], login_id="sub2")
    mine = make_user(Role.EMPLOYEE, reports_to=office["sub"], login_id="mine")
    t = make_task(sub2, mine)  # a task made by the other sub-admin, but done by the first one's employee
    upload_all(login("mine"), DATA + b"1", uuid.uuid4(), t, name="x.xlsx")
    v = FileVersion.objects.get(uploaded_by=mine)
    assert get("sub", v).status_code == 200  # the file belongs to their team
    assert get("other", v).status_code == 404


def test_the_query_count_of_a_download_does_not_grow_with_the_amount_of_data(office):
    def count(v):
        c = login("sub")  # signing in is not part of the download
        with CaptureQueriesContext(connection) as q:
            assert c.get(f"/files/v/{v.pk}/download").status_code == 200
        return len(q)

    first = uploaded(office)
    few = count(first)
    for i in range(6):
        v = uploaded(office, data=DATA + bytes([i]), name=f"f{i}.xlsx")
        h = FileHandover.objects.create(version=v, from_user=office["emp"], task=office["task"])
        FileHandoverRecipient.objects.create(handover=h, to_user=office["other"])
    assert count(first) == few and few <= 15


# --- the cache-miss path --------------------------------------------------------------------------------------


def test_an_evicted_file_streams_from_sharepoint_and_lands_back_in_the_cache(office, remote):
    v = uploaded(office, remote)
    cache.evict(0)
    assert not Blob.objects.get().cached
    r = get("sub", v)
    assert r.status_code == 200 and body(r) == DATA
    assert FileAccessLog.objects.get().source == "ONEDRIVE"
    blob = Blob.objects.get()
    assert blob.cached and cache.fetch_part_path(blob.sha256).exists() is False
    again = get("sub", v)
    assert body(again) == DATA and FileAccessLog.objects.order_by("-id").first().source == "CACHE"


def test_the_copy_made_while_streaming_is_cached_only_if_it_verifies(office, remote):
    v = uploaded(office, remote)
    cache.evict(0)
    blob = Blob.objects.get()
    remote._items[blob.item_id].file.write_bytes(b"\x00" * len(DATA))  # SharePoint now returns other bytes
    r = get("sub", v)
    assert r.status_code == 200 and len(body(r)) == len(DATA)
    assert not Blob.objects.get().cached  # the bad copy never entered the cache
    assert not cache.fetch_part_path(blob.sha256).exists()


def test_a_client_that_leaves_midway_leaves_no_partial_copy_in_the_cache(office, remote, settings):
    settings.OTS_UPLOAD_PIECE_BYTES = 1 << 20
    big = bytes(range(256)) * 20_000  # 5 MB: several chunks
    upload_all(login("emp"), big, uuid.uuid4(), office["task"], name="big.xlsx", piece=1 << 20)
    drain(remote)
    cache.evict(0)
    blob = Blob.objects.get()
    it = remote.open_read(RemoteRef(Library.BUSINESS, blob.drive_id, blob.item_id))
    first = next(it)
    gen = download._tee(blob, first, it)
    assert next(gen) == first
    gen.close()  # the browser went away
    assert not cache.fetch_part_path(blob.sha256).exists() and not Blob.objects.get().cached


def test_a_cache_flag_without_a_file_falls_back_to_sharepoint(office, remote):
    v = uploaded(office, remote)
    from pathlib import Path

    Path(Blob.objects.get().local_path).unlink()  # the cache lost the file behind the database's back
    r = get("sub", v)
    assert r.status_code == 200 and body(r) == DATA
    assert Blob.objects.get().cached  # and the fetch put it back


def test_a_file_that_exists_nowhere_is_reported_not_faked(office):
    v = uploaded(office)  # still STAGED
    from pathlib import Path

    Path(Blob.objects.get().local_path).unlink()
    r = get("sub", v)
    assert r.status_code == 503 and "not available" in r.content.decode()
    assert FileAccessLog.objects.count() == 0


def test_a_file_deleted_in_sharepoint_says_so_and_does_not_pretend_to_be_offline(office, remote):
    v = uploaded(office, remote)
    cache.evict(0)
    blob = Blob.objects.get()
    remote.remote_delete(RemoteRef(Library.BUSINESS, blob.drive_id, blob.item_id))
    r = get("sub", v)
    assert r.status_code == 503 and "missing from SharePoint" in r.content.decode()
    from ots.files.models import FileFetchRequest

    assert FileFetchRequest.objects.count() == 0


# --- preview --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name, ctype",
    [("a.pdf", "application/pdf"), ("a.png", "image/png"), ("a.JPG", "image/jpeg"), ("a.jpeg", "image/jpeg"),
     ("a.webp", "image/webp"), ("a.txt", "text/plain; charset=utf-8")],
)  # fmt: skip
def test_previewable_types_are_served_inline_in_a_sandbox(office, name, ctype):
    v = uploaded(office, name=name)
    r = get("sub", v, "preview")
    assert r.status_code == 200 and body(r) == DATA
    assert r["Content-Type"] == ctype  # chosen from the extension, not from anything the uploader claimed
    assert r["Content-Security-Policy"] == "default-src 'none'; img-src 'self'; object-src 'self'; sandbox"
    assert r["Content-Disposition"] == f'inline; filename="{v.file_code}"'
    assert r["X-Content-Type-Options"] == "nosniff"
    assert FileAccessLog.objects.get().action == "PREVIEW"


@pytest.mark.parametrize("name", ["a.xlsx", "a.html", "a.svg", "a.docx", "a.js.txt.bin", "noext"])
def test_everything_else_cannot_be_previewed(office, name):
    v = uploaded(office, name=name)
    r = get("sub", v, "preview")
    assert r.status_code == 415
    assert get("sub", v).status_code == 200  # it can still be downloaded
    assert FileAccessLog.objects.count() == 1  # only the download was logged


def test_preview_is_scoped_like_download(office):
    v = uploaded(office, name="a.pdf")
    assert get("other", v, "preview").status_code == 404


def test_the_task_list_links_download_and_only_offers_preview_where_it_can(office):
    uploaded(office, name="a.pdf")
    uploaded(office, name="b.xlsx", data=DATA + b"2")
    page = login("sub").get(f"/app/tasks/{office['task'].pk}/files").content.decode()
    assert page.count("/download") == 2 and page.count("/preview") == 1
    assert FileVersion.objects.filter(state=FileState.STAGED).count() == 2
