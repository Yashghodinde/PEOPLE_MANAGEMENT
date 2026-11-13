"""M08.1: the file tables hold their rules in the database, and the access log is append-only."""

import uuid

import pytest
from django.db import DatabaseError, IntegrityError, connection, transaction
from ots.accounts.models import Role
from ots.files.models import FileAccessLog, FileVersion, UploadSession

from tests.accounts.factories import make_user
from tests.files.factories import make_blob, make_file

pytestmark = pytest.mark.django_db


@pytest.fixture
def boss():
    return make_user(Role.SUPER_ADMIN)


def test_a_version_number_is_unique_per_file(boss):
    f, v = make_file(boss)
    with pytest.raises(IntegrityError), transaction.atomic():
        FileVersion.objects.create(
            file=f, version_number=1, file_code="other-code", original_filename="a", blob=make_blob(),
            size_bytes=1, uploaded_by=boss, idempotency_key=uuid.uuid4(),
        )  # fmt: skip


def test_a_file_code_and_an_idempotency_key_are_each_unique(boss):
    f, v = make_file(boss)
    kw = {"file": f, "original_filename": "a", "blob": make_blob(), "size_bytes": 1, "uploaded_by": boss}
    with pytest.raises(IntegrityError), transaction.atomic():
        FileVersion.objects.create(
            version_number=2, file_code=v.file_code, idempotency_key=uuid.uuid4(), **kw
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        FileVersion.objects.create(version_number=2, file_code="new", idempotency_key=v.idempotency_key, **kw)


def test_an_upload_session_cannot_receive_more_than_it_expects(boss):
    with pytest.raises(IntegrityError), transaction.atomic():
        UploadSession.objects.create(
            idempotency_key=uuid.uuid4(), user=boss, expected_size=10, expected_sha256="a" * 64,
            original_filename="a.txt", received_bytes=11,
        )  # fmt: skip


def test_a_blob_key_must_be_a_real_sha256():
    from ots.files.models import Blob

    with pytest.raises(IntegrityError), transaction.atomic():
        Blob.objects.create(sha256="NOT-A-HASH", size_bytes=1)


def test_a_version_blocks_deleting_its_blob_and_its_file(boss):
    from django.db.models import ProtectedError

    f, v = make_file(boss)
    with pytest.raises(ProtectedError):
        v.blob.delete()
    with pytest.raises(ProtectedError):
        f.delete()


def test_the_access_log_rejects_update_and_delete(boss):
    f, v = make_file(boss)
    row = FileAccessLog.objects.create(
        version=v, user=boss, action=FileAccessLog.Action.DOWNLOAD, source=FileAccessLog.Source.CACHE
    )
    for sql in (
        "UPDATE file_access_log SET action='PREVIEW' WHERE id=%s",
        "DELETE FROM file_access_log WHERE id=%s",
    ):
        with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cur:
            cur.execute(sql, [row.pk])
