"""M08.5 qualification: search stays under 200 ms with 100,000 file versions (run with `pytest -m slow`)."""

import time
import uuid

import pytest
from django.db import connection
from ots.accounts.models import Role
from ots.files import selectors
from ots.files.models import Blob, File, FileVersion, TaskFile

from tests.accounts.factories import company, make_user
from tests.tasks.factories import make_task

pytestmark = [pytest.mark.django_db, pytest.mark.slow]
N = 100_000
WORDS = ["budget", "invoice", "report", "survey", "contract", "photo", "sales", "audit", "plan", "notes"]


def test_search_under_200ms_at_100k_versions():
    boss = make_user(Role.SUPER_ADMIN, login_id="boss", full_name="Boss Person")
    sub = make_user(Role.SUB_ADMIN, reports_to=boss, login_id="sub", full_name="Sub Admin")
    emp = make_user(Role.EMPLOYEE, reports_to=sub, login_id="emp", full_name="Rahul Sharma")
    tasks = [make_task(sub, emp, title=f"{WORDS[i % 10]} job {i}") for i in range(50)]
    blob = Blob.objects.create(sha256="a" * 64, size_bytes=1)
    co = company()
    files = File.objects.bulk_create(
        [File(company=co, display_name=f"f{i}", owner=emp) for i in range(N // 4)]
    )
    versions = [
        FileVersion(
            file=files[i // 4], version_number=i % 4 + 1, file_code=f"2026-{i:07d}-{WORDS[i % 10]}-{uuid.uuid4().hex[:8]}.pdf",
            original_filename=f"{WORDS[i % 10]} {WORDS[(i // 10) % 10]} {i}.pdf", blob=blob, size_bytes=1,
            uploaded_by=emp if i % 3 else sub, idempotency_key=uuid.uuid4(),
        )
        for i in range(N)
    ]  # fmt: skip
    FileVersion.objects.bulk_create(versions, batch_size=5000)
    sample = FileVersion.objects.order_by("id")[: N // 2]
    TaskFile.objects.bulk_create(
        [TaskFile(task=tasks[j % 50], file_id=v.file_id, version=v, relation="SUBMISSION", linked_by=emp)
         for j, v in enumerate(sample)],
        batch_size=5000,
    )  # fmt: skip
    with connection.cursor() as cur:
        cur.execute("ANALYZE file_versions; ANALYZE task_files; ANALYZE users; ANALYZE tasks")
    cases = [
        (boss, "budget"), (boss, "survey plan 1234"), (emp, "contract"), (sub, "rahul audit"),
        (boss, "job 7"), (emp, "zzzzqq"), (boss, "0099999"),
    ]  # fmt: skip
    for viewer, q in cases:
        t0 = time.perf_counter()
        selectors.search(viewer, q)
        took = (time.perf_counter() - t0) * 1000
        print(f"search {q!r} as {viewer.role}: {took:.0f} ms")
        assert took < 200, f"{q!r} as {viewer.role} took {took:.0f} ms"
