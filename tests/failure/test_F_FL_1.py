"""F-FL-1: upload interrupted. After three pieces the connection drops; starting again with the same key finds
the upload where it stopped and continues from received_bytes. The finished file is intact and there is exactly
one version."""

import uuid

import pytest
from ots.files import layout
from ots.files.models import FileVersion, UploadSession

from tests.files.conftest import login, put, sha, start

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("dirs")]
DATA = bytes((i * 7) % 256 for i in range(10 * 1024 + 300))  # ten full pieces and a short last one


def test_interrupt_after_three_pieces_then_resume_with_the_same_key(office):
    c = login("emp")
    key = uuid.uuid4()
    uid = start(c, DATA, key, office["task"]).json()["upload_id"]
    for i in range(3):
        assert put(c, uid, DATA, i * 1024, i * 1024 + 1023).status_code == 200
    # a half piece arrives and the connection dies: the bytes are on disk but not counted
    with layout.staging_path(uid).open("ab") as fh:
        fh.write(b"JUNK" * 100)

    again = login("emp")  # a new browser session, the same upload key
    r = start(again, DATA, key, office["task"])
    assert r.status_code == 200 and r.json()["upload_id"] == uid and r.json()["received_bytes"] == 3072
    assert again.get(f"/files/uploads/{uid}", HTTP_ACCEPT="application/json").json()["received_bytes"] == 3072

    a = 3072
    while a < len(DATA):
        b = min(a + 1024, len(DATA)) - 1
        last = put(again, uid, DATA, a, b)
        a = b + 1
    assert last.status_code == 201
    v = FileVersion.objects.get()
    assert layout.blob_path(sha(DATA)).read_bytes() == DATA  # the junk tail never reached the file
    assert v.size_bytes == len(DATA) and UploadSession.objects.count() == 1


def test_a_piece_at_the_wrong_offset_is_answered_with_416_and_the_right_offset(office):
    c = login("emp")
    uid = start(c, DATA, uuid.uuid4(), office["task"]).json()["upload_id"]
    put(c, uid, DATA, 0, 1023)
    ahead = put(c, uid, DATA, 2048, 3071)  # a piece was lost: this one skips ahead
    assert ahead.status_code == 416 and ahead.json()["received_bytes"] == 1024
    behind = put(c, uid, DATA, 0, 1023)  # a piece the server already has
    assert behind.status_code == 416 and behind.json()["received_bytes"] == 1024
    assert UploadSession.objects.get().received_bytes == 1024
    assert layout.staging_path(uid).stat().st_size == 1024  # a refused piece wrote nothing
