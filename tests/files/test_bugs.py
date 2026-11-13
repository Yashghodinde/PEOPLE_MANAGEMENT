"""M08 bug hunt: awkward inputs and races found by reading the code with suspicion."""

import uuid

import pytest
from django.test import Client
from ots.files import layout, services
from ots.files.blocklist import is_blocked
from ots.files.models import Blob, FileVersion, UploadSession

from tests.files.conftest import PW, login, put, sha, start, upload_all

pytestmark = pytest.mark.django_db
DATA = bytes(range(256)) * 12


@pytest.mark.parametrize("name", ["tool.exe.", "tool.exe ", "tool.exe. .", "TOOL.EXE...", "a.b.ps1 "])
def test_trailing_dots_and_spaces_do_not_hide_a_blocked_type(name):
    assert is_blocked(name)  # Windows ignores them, so the program would still run


def test_a_very_long_name_or_id_still_uploads(office):
    office["emp"].full_name = "y" * 190
    office["emp"].save(update_fields=["full_name"])
    c = login("emp")
    _, last = upload_all(c, DATA, uuid.uuid4(), office["task"], name="n" * 200 + ".xlsx")
    assert last.status_code == 201
    assert len(FileVersion.objects.get().file_code) <= 255


def test_allow_blocked_must_be_a_real_true(office):
    r = start(login("boss"), DATA, uuid.uuid4(), office["task"], name="x.exe", allow_blocked="false")
    assert r.status_code == 422


def test_a_lost_part_file_is_noticed_instead_of_zero_filled(office):
    c = login("emp")
    uid = start(c, DATA, uuid.uuid4(), office["task"]).json()["upload_id"]
    put(c, uid, DATA, 0, 1023)
    layout.staging_path(uid).unlink()  # the disk lost it (cleanup, restore from backup)
    r = put(c, uid, DATA, 1024, 2047)
    assert r.status_code == 416 and r.json()["received_bytes"] == 0
    for a in (0, 1024, 2048):  # starting over from the offset the server gave finishes cleanly
        last = put(c, uid, DATA, a, a + 1023)
    assert last.status_code == 201
    assert layout.blob_path(sha(DATA)).read_bytes() == DATA


def test_a_truncated_part_file_resumes_from_what_is_really_there(office):
    c = login("emp")
    uid = start(c, DATA, uuid.uuid4(), office["task"]).json()["upload_id"]
    put(c, uid, DATA, 0, 1023)
    put(c, uid, DATA, 1024, 2047)
    with layout.staging_path(uid).open("r+b") as fh:
        fh.truncate(1500)
    r = put(c, uid, DATA, 2048, 3071)
    assert r.status_code == 416 and r.json()["received_bytes"] == 1500


def test_csrf_is_enforced_on_pieces_and_a_token_makes_them_work(office):
    c = Client(enforce_csrf_checks=True)
    page = c.get("/login")
    token = page.cookies["csrftoken"].value
    assert (
        c.post("/login", {"login_id": "emp", "password": PW, "csrfmiddlewaretoken": token}).status_code == 302
    )
    token = c.cookies["csrftoken"].value
    body = {
        "idempotency_key": str(uuid.uuid4()), "original_filename": "a.txt", "size": len(DATA),
        "sha256": sha(DATA), "task_id": str(office["task"].pk),
    }  # fmt: skip
    kw = {"content_type": "application/json", "HTTP_ACCEPT": "application/json"}
    assert c.post("/files/uploads", body, **kw).status_code == 403  # no token: refused
    r = c.post("/files/uploads", body, HTTP_X_CSRFTOKEN=token, **kw)
    assert r.status_code == 201
    uid = r.json()["upload_id"]
    hdr = {"HTTP_CONTENT_RANGE": "bytes 0-1023/3072", "content_type": "application/octet-stream"}
    assert c.put(f"/files/uploads/{uid}", DATA[:1024], **hdr).status_code == 403
    assert c.put(f"/files/uploads/{uid}", DATA[:1024], HTTP_X_CSRFTOKEN=token, **hdr).status_code == 200


def test_two_people_uploading_the_same_new_content_at_once_both_succeed(office):
    """The blob row is created by whoever commits first; the other must reuse it, not crash."""
    a, b = login("emp"), login("other")

    from tests.tasks.factories import make_task

    t2 = make_task(office["sub"], office["other"])
    uid_a = start(a, DATA, uuid.uuid4(), office["task"]).json()["upload_id"]
    uid_b = start(b, DATA, uuid.uuid4(), t2).json()["upload_id"]
    for a_, b_ in ((0, 1023), (1024, 2047)):
        put(a, uid_a, DATA, a_, b_)
        put(b, uid_b, DATA, a_, b_)
    # b's commit finds no blob row at first (a has not committed): simulate a's commit landing in between
    real = Blob.objects.select_for_update
    state = {"done": False}

    def late(*args, **kw):
        qs = real(*args, **kw)
        if not state["done"]:
            state["done"] = True
            put(a, uid_a, DATA, 2048, 3071)  # a commits now, while b is mid-commit
            return qs.none()
        return qs

    original = Blob.objects.select_for_update
    try:
        Blob.objects.select_for_update = late  # type: ignore[method-assign]
        r = put(b, uid_b, DATA, 2048, 3071)
    finally:
        Blob.objects.select_for_update = original  # type: ignore[method-assign]
    assert r.status_code == 201, r.content
    assert Blob.objects.count() == 1 and FileVersion.objects.count() == 2


def test_a_closed_upload_key_is_reported_not_silently_resumed(office):
    c = login("emp")
    key = uuid.uuid4()
    uid = start(c, DATA, key, office["task"]).json()["upload_id"]
    c.delete(f"/files/uploads/{uid}", HTTP_ACCEPT="application/json")
    again = start(c, DATA, key, office["task"])
    assert again.json()["state"] == "ABANDONED"  # the client sees it is closed and uses a fresh key


def test_a_second_start_with_the_same_key_but_different_content_is_refused(office):
    c = login("emp")
    key = uuid.uuid4()
    start(c, DATA, key, office["task"])
    r = start(c, DATA + b"more", key, office["task"])
    assert r.status_code == 409


def test_a_hundred_pieces_in_a_row_stay_in_sync(office, settings):
    settings.OTS_UPLOAD_PIECE_BYTES = 64
    data = bytes(range(256)) * 25  # 6400 bytes, 100 pieces
    c = login("emp")
    uid, last = upload_all(c, data, uuid.uuid4(), office["task"], piece=64)
    assert last.status_code == 201 and layout.blob_path(sha(data)).read_bytes() == data
    assert UploadSession.objects.get(pk=uid).received_bytes == len(data)


def test_concurrent_pieces_at_the_same_offset_only_one_wins(office):
    c = login("emp")
    uid = start(c, DATA, uuid.uuid4(), office["task"]).json()["upload_id"]
    session = UploadSession.objects.get(pk=uid)
    # the row lock makes the second caller wait, then see the new offset
    services.receive_piece(
        session.user, session.pk, 0, 1023, len(DATA), __import__("io").BytesIO(DATA[:1024])
    )
    from ots.files.errors import OffsetMismatch

    with pytest.raises(OffsetMismatch):
        services.receive_piece(
            session.user, session.pk, 0, 1023, len(DATA), __import__("io").BytesIO(DATA[:1024])
        )
