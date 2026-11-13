"""M08.2: the upload endpoints and the commit transaction."""

import os
import uuid

import pytest
from ots.audit.models import AuditLog
from ots.core.models import OutboxEvent
from ots.files import layout
from ots.files.models import Blob, File, FileState, FileVersion, TaskFile, UploadSession
from ots.storage.models import StorageOperation as Op
from ots.tasks.models import TaskEvent

from tests.files.conftest import login, put, sha, start, upload_all

pytestmark = pytest.mark.django_db
DATA = bytes(range(256)) * 12  # 3072 bytes = three 1 KiB pieces


def test_a_whole_upload_commits_everything_in_one_go(office, django_capture_on_commit_callbacks):
    c = login("emp")
    with django_capture_on_commit_callbacks(execute=True):
        uid, last = upload_all(c, DATA, uuid.uuid4(), office["task"], name="Sales Sheet.XLSX")
    body = last.json()
    assert last.status_code == 201 and body["version"] == 1 and body["archived"] is False
    v = FileVersion.objects.get()
    assert v.state == FileState.STAGED and v.size_bytes == len(DATA) and v.uploaded_by == office["emp"]
    emp = office["emp"]
    assert f"_{emp.employee_id}-RahulS_{office['task'].task_code}-monthly-sales-report_v1.xlsx" in v.file_code
    assert v.file_code.endswith("_v1.xlsx") and v.mime_type.startswith("application/")
    f = File.objects.get()
    assert f.current_version == v and f.display_name == "Sales Sheet.XLSX"
    link = TaskFile.objects.get()
    assert (
        link.task == office["task"]
        and link.relation == "SUBMISSION"
        and link.assignment.user == office["emp"]
    )
    assert TaskEvent.objects.filter(event_type="file.version_added", task=office["task"]).count() == 1
    assert AuditLog.objects.filter(action="file.uploaded", actor_id=office["emp"].pk).count() == 1
    kinds = set(OutboxEvent.objects.values_list("type", flat=True))
    assert {"file.version_added", "file.staged"} <= kinds
    blob = Blob.objects.get()
    assert blob.sha256 == sha(DATA) and blob.cached and blob.state == FileState.STAGED
    assert layout.blob_path(blob.sha256).read_bytes() == DATA
    assert not layout.staging_path(uid).exists()  # the part file is gone after the commit
    op = Op.objects.get()
    assert op.kind == "ARCHIVE_FILE" and op.sha256 == blob.sha256 and op.ref_id == str(v.pk)
    assert op.local_path == str(layout.blob_path(blob.sha256)) and op.remote_path.endswith(v.file_code)
    assert UploadSession.objects.get().state == FileState.STAGED


def test_the_last_piece_sent_twice_returns_the_same_version(office):
    c = login("emp")
    key = uuid.uuid4()
    uid, last = upload_all(c, DATA, key, office["task"])
    again = put(c, uid, DATA, 2048, 3071)
    assert again.status_code == 201 and again.json()["version_id"] == last.json()["version_id"]
    assert FileVersion.objects.count() == 1


def test_same_content_twice_keeps_one_blob_and_one_archive_operation(office):
    c = login("emp")
    upload_all(c, DATA, uuid.uuid4(), office["task"], name="a.xlsx")
    upload_all(c, DATA, uuid.uuid4(), office["task"], name="b.xlsx")
    assert Blob.objects.count() == 1 and FileVersion.objects.count() == 2 and File.objects.count() == 2
    assert Op.objects.count() == 1  # the second version rides on the first archive
    assert len([p for p in layout.blob_path(sha(DATA)).parent.iterdir()]) == 1


def test_content_already_archived_makes_a_version_that_is_archived_at_once(office):
    c = login("emp")
    upload_all(c, DATA, uuid.uuid4(), office["task"], name="a.xlsx")
    Blob.objects.update(state=FileState.ARCHIVED)
    Op.objects.update(state=Op.State.DONE)
    _, last = upload_all(c, DATA, uuid.uuid4(), office["task"], name="b.xlsx")
    assert last.json()["archived"] is True
    assert Op.objects.count() == 1 and FileVersion.objects.latest("uploaded_at").state == FileState.ARCHIVED


def test_an_evicted_blob_is_cached_again_by_a_new_upload(office):
    c = login("emp")
    upload_all(c, DATA, uuid.uuid4(), office["task"])
    path = layout.blob_path(sha(DATA))
    path.unlink()
    Blob.objects.update(cached=False, local_path=None, state=FileState.ARCHIVED)
    upload_all(c, DATA, uuid.uuid4(), office["task"], name="again.xlsx")
    blob = Blob.objects.get()
    assert blob.cached and blob.local_path == str(path) and path.read_bytes() == DATA


def test_a_new_version_of_a_file_is_a_second_version_with_its_own_code(office):
    c = login("emp")
    upload_all(c, DATA, uuid.uuid4(), office["task"])
    f = File.objects.get()
    v2 = DATA + b"edited"
    _, last = upload_all(c, v2, uuid.uuid4(), office["task"], file_id=str(f.pk))
    assert last.json()["version"] == 2 and File.objects.count() == 1
    f.refresh_from_db()
    assert f.current_version.version_number == 2 and f.versions.count() == 2
    assert f.current_version.file_code.endswith("_v2.xlsx")


def test_a_file_code_collision_gets_a_suffix(office, monkeypatch):
    c = login("emp")
    from ots.files import services

    monkeypatch.setattr(services, "make_code", lambda **kw: "same-code.xlsx")
    upload_all(c, DATA, uuid.uuid4(), office["task"])
    upload_all(c, DATA + b"x", uuid.uuid4(), office["task"])
    assert set(FileVersion.objects.values_list("file_code", flat=True)) == {
        "same-code.xlsx",
        "same-code-2.xlsx",
    }


# --- who may upload -------------------------------------------------------------------------------------------


def test_people_outside_the_task_cannot_start_an_upload(office):
    r = start(login("other"), DATA, uuid.uuid4(), office["task"])
    assert r.status_code == 404  # an invisible task answers like a missing one
    assert UploadSession.objects.count() == 0


def test_a_sub_admin_and_the_super_admin_can_upload_to_a_visible_task(office):
    for who in ("sub", "boss"):
        r = start(login(who), DATA, uuid.uuid4(), office["task"], relation="DELIVERABLE")
        assert r.status_code == 201, (who, r.content)


def test_an_employee_may_only_upload_submissions(office):
    r = start(login("emp"), DATA, uuid.uuid4(), office["task"], relation="REFERENCE")
    assert r.status_code == 403
    assert AuditLog.objects.filter(action="security.denied").exists()


def test_an_employee_cannot_use_someone_elses_assignment(office):
    from tests.tasks.factories import make_task

    t2 = make_task(office["sub"], office["other"])
    other_assignment = t2.assignments.get()
    r = start(login("emp"), DATA, uuid.uuid4(), office["task"], assignment_id=str(other_assignment.pk))
    assert r.status_code == 403


def test_only_the_super_admin_uploads_to_the_library(office):
    assert start(login("sub"), DATA, uuid.uuid4()).status_code == 403
    r = start(login("boss"), DATA, uuid.uuid4())
    assert r.status_code == 201


def test_a_file_from_another_task_cannot_be_given_a_new_version(office):
    from tests.tasks.factories import make_task

    t2 = make_task(office["sub"], office["emp"], title="Other job")
    c = login("emp")
    upload_all(c, DATA, uuid.uuid4(), t2)
    r = start(c, DATA + b"1", uuid.uuid4(), office["task"], file_id=str(File.objects.get().pk))
    assert r.status_code == 422


def test_another_person_cannot_pick_up_my_upload(office):
    r = start(login("emp"), DATA, key := uuid.uuid4(), office["task"])
    uid = r.json()["upload_id"]
    sub = login("sub")
    assert sub.get(f"/files/uploads/{uid}", HTTP_ACCEPT="application/json").status_code == 404
    assert put(sub, uid, DATA, 0, 1023).status_code == 404
    assert start(sub, DATA, key, office["task"]).status_code == 403  # the same key from someone else


# --- validation -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "patch",
    [
        {"size": 0},
        {"size": -5},
        {"size": "abc"},
        {"size": 600 * 1024 * 1024},
        {"sha256": "xyz"},
        {"original_filename": ""},
        {"original_filename": ".."},
        {"relation": "NONSENSE"},
        {"idempotency_key": "not-a-uuid"},
        {"idempotency_key": ""},
        {"task_id": "nope"},
    ],
)
def test_bad_start_requests_are_refused_and_create_nothing(office, patch):
    c = login("emp")
    body = {
        "idempotency_key": str(uuid.uuid4()), "original_filename": "a.txt", "size": 10,
        "sha256": sha(b"0123456789"), "task_id": str(office["task"].pk), **patch,
    }  # fmt: skip
    r = c.post("/files/uploads", body, content_type="application/json", HTTP_ACCEPT="application/json")
    assert r.status_code in (404, 422), r.content
    assert UploadSession.objects.count() == 0


def test_a_path_in_the_file_name_is_reduced_to_the_name(office):
    r = start(login("emp"), DATA, uuid.uuid4(), office["task"], name="C:\\Users\\x\\..\\secret.txt")
    assert r.status_code == 201
    assert UploadSession.objects.get().original_filename == "secret.txt"


def test_pieces_with_a_bad_range_header_are_refused(office):
    c = login("emp")
    uid = start(c, DATA, uuid.uuid4(), office["task"]).json()["upload_id"]
    bad = c.put(
        f"/files/uploads/{uid}",
        data=b"x",
        content_type="application/octet-stream",
        HTTP_ACCEPT="application/json",
    )
    assert bad.status_code == 422
    for a, b, total in [(0, 5000, 3072), (0, 2000, 3072), (5, 4, 3072), (0, 1023, 99)]:
        r = c.put(
            f"/files/uploads/{uid}", data=b"x" * 1024, content_type="application/octet-stream",
            HTTP_CONTENT_RANGE=f"bytes {a}-{b}/{total}", HTTP_ACCEPT="application/json",
        )  # fmt: skip
        assert r.status_code == 422, (a, b, total, r.content)


def test_a_piece_shorter_than_its_range_is_refused_and_leaves_the_offset_alone(office):
    c = login("emp")
    uid = start(c, DATA, uuid.uuid4(), office["task"]).json()["upload_id"]
    r = c.put(
        f"/files/uploads/{uid}", data=b"x" * 100, content_type="application/octet-stream",
        HTTP_CONTENT_RANGE="bytes 0-1023/3072", HTTP_ACCEPT="application/json",
    )  # fmt: skip
    assert r.status_code == 422
    assert UploadSession.objects.get().received_bytes == 0
    assert os.path.getsize(layout.staging_path(uid)) == 0
    assert put(c, uid, DATA, 0, 1023).json()["received_bytes"] == 1024  # and the real piece still works


def test_a_blocked_type_is_refused_but_the_super_admin_may_override_with_an_audit_row(office):
    assert start(login("emp"), DATA, uuid.uuid4(), office["task"], name="tool.EXE").status_code == 422
    assert (
        start(
            login("sub"), DATA, uuid.uuid4(), office["task"], name="tool.exe", allow_blocked=True
        ).status_code
        == 422
    )
    r = start(login("boss"), DATA, uuid.uuid4(), office["task"], name="tool.exe", allow_blocked=True)
    assert r.status_code == 201
    assert AuditLog.objects.filter(action="file.block_override", actor_id=office["sa"].pk).count() == 1


def test_start_requires_login(office):
    from django.test import Client

    r = Client().post("/files/uploads", "{}", content_type="application/json")
    assert r.status_code == 302


def test_cancel_closes_the_upload_and_removes_the_part(office):
    c = login("emp")
    uid = start(c, DATA, uuid.uuid4(), office["task"]).json()["upload_id"]
    put(c, uid, DATA, 0, 1023)
    assert layout.staging_path(uid).exists()
    assert c.delete(f"/files/uploads/{uid}", HTTP_ACCEPT="application/json").status_code == 200
    assert not layout.staging_path(uid).exists()
    assert UploadSession.objects.get().state == FileState.ABANDONED
    assert put(c, uid, DATA, 1024, 2047).status_code == 409  # a closed upload takes no more pieces
