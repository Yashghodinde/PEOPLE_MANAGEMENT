"""F-FL-2: hash mismatch at the end of an upload. The bytes that arrived are not the bytes the browser
fingerprinted. The upload is REJECTED, the part file is deleted, and no file, version, blob, link, operation or
event exists."""

import uuid

import pytest
from ots.core.models import OutboxEvent
from ots.files import layout
from ots.files.models import Blob, File, FileState, FileVersion, TaskFile, UploadSession
from ots.storage.models import StorageOperation as Op

from tests.files.conftest import login, put, start

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("dirs")]
DATA = bytes(range(256)) * 12


def test_wrong_bytes_are_rejected_and_leave_no_record(office):
    c = login("emp")
    uid = start(c, DATA, uuid.uuid4(), office["task"]).json()["upload_id"]
    damaged = bytearray(DATA)
    damaged[1500] ^= 0xFF  # one flipped byte, as a bad network or a failing disk might do
    damaged = bytes(damaged)
    assert put(c, uid, damaged, 0, 1023).status_code == 200
    assert put(c, uid, damaged, 1024, 2047).status_code == 200
    last = put(c, uid, damaged, 2048, 3071)
    assert last.status_code == 422 and last.json()["error"] == "hash_mismatch"
    assert UploadSession.objects.get().state == FileState.REJECTED
    assert not layout.staging_path(uid).exists()
    for model in (File, FileVersion, Blob, TaskFile, Op):
        assert model.objects.count() == 0, model
    assert not OutboxEvent.objects.filter(type__startswith="file.").exists()
    assert not layout.blob_path(__import__("hashlib").sha256(damaged).hexdigest()).exists()
    assert put(c, uid, DATA, 0, 1023).status_code == 409  # a rejected upload cannot be continued


def test_a_fresh_key_after_a_rejection_uploads_normally(office):
    c = login("emp")
    uid = start(c, DATA, uuid.uuid4(), office["task"]).json()["upload_id"]
    for a in (0, 1024, 2048):
        put(c, uid, b"\x00" * len(DATA), a, a + 1023)
    uid2 = start(c, DATA, uuid.uuid4(), office["task"]).json()["upload_id"]
    for a in (0, 1024, 2048):
        last = put(c, uid2, DATA, a, a + 1023)
    assert last.status_code == 201 and FileVersion.objects.count() == 1
