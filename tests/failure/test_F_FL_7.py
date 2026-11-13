"""F-FL-7: a new version is never a retry. The same Idempotency-Key always yields the same version, however
often the browser repeats the request; a deliberate new upload (new key) is version N+1."""

import uuid

import pytest
from ots.files.models import File, FileVersion, UploadSession

from tests.files.conftest import login, start, upload_all

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("dirs")]
DATA = bytes(range(256)) * 12


def test_the_same_key_never_makes_a_second_version(office):
    c = login("emp")
    key = uuid.uuid4()
    uid, first = upload_all(c, DATA, key, office["task"])
    for _ in range(3):  # the browser retries the whole upload (a double click, a reload, a flaky network)
        r = start(c, DATA, key, office["task"])
        assert r.status_code == 200
        assert r.json()["upload_id"] == uid and r.json()["version_id"] == first.json()["version_id"]
    assert FileVersion.objects.count() == 1 and UploadSession.objects.count() == 1


def test_a_new_key_is_version_n_plus_1(office):
    c = login("emp")
    upload_all(c, DATA, uuid.uuid4(), office["task"])
    file_id = str(File.objects.get().pk)
    numbers = []
    for i in range(3):
        _, last = upload_all(c, DATA + bytes([i]), uuid.uuid4(), office["task"], file_id=file_id)
        numbers.append(last.json()["version"])
    assert numbers == [2, 3, 4]
    assert list(FileVersion.objects.order_by("version_number").values_list("version_number", flat=True)) == [
        1,
        2,
        3,
        4,
    ]


def test_two_simultaneous_starts_with_one_key_make_one_session(office, monkeypatch):
    c = login("emp")
    key = uuid.uuid4()
    start(c, DATA, key, office["task"])
    real = UploadSession.objects.filter
    calls = {"n": 0}

    def racy(*a, **k):  # the first lookup misses (the other request has not committed yet)
        calls["n"] += 1
        qs = real(*a, **k)
        return qs.none() if calls["n"] == 1 else qs

    monkeypatch.setattr(UploadSession.objects, "filter", racy)
    r = start(c, DATA, key, office["task"])
    assert r.status_code == 200 and UploadSession.objects.count() == 1
