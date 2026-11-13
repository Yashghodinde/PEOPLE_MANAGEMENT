"""Build files, blobs and versions directly through the ORM (the services have their own tests)."""

from __future__ import annotations

import hashlib
import itertools
import uuid

from ots.accounts.models import User
from ots.files.models import Blob, File, FileVersion

from tests.accounts.factories import company

_n = itertools.count(1)


def make_blob(data: bytes | None = None) -> Blob:
    data = data if data is not None else f"blob {next(_n)}".encode()
    sha = hashlib.sha256(data).hexdigest()
    blob, _ = Blob.objects.get_or_create(sha256=sha, defaults={"size_bytes": len(data)})
    return blob


def make_file(owner: User, name: str = "report.xlsx") -> tuple[File, FileVersion]:
    blob = make_blob()
    f = File.objects.create(company=company(), display_name=name, owner=owner)
    v = FileVersion.objects.create(
        file=f,
        version_number=1,
        file_code=f"code-{uuid.uuid4().hex}.xlsx",
        original_filename=name,
        blob=blob,
        size_bytes=blob.size_bytes,
        uploaded_by=owner,
        idempotency_key=uuid.uuid4(),
    )
    f.current_version = v
    f.save(update_fields=["current_version"])
    return f, v
