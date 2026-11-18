"""IMP-13.3: Team Storage with a wide team: 50 reports, 500 versions. The page costs the same number of queries as a
small one, and nobody outside the chain sees any of it."""

import datetime
import uuid

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from ots.accounts.models import Role
from ots.files.models import Blob, File, FileVersion, TaskFile
from ots.tasks.models import TaskAssignment

from tests.accounts.factories import company, make_user
from tests.files.conftest import login
from tests.tasks.factories import make_task

pytestmark = pytest.mark.django_db


def base():
    boss = make_user(Role.SUPER_ADMIN, login_id="boss")
    mgr = make_user(Role.SUB_ADMIN, reports_to=boss, login_id="mgr")
    make_user(Role.SUB_ADMIN, reports_to=boss, login_id="mgr2")
    return mgr


def add_people(mgr, start: int, reports: int, per_person: int):
    blob = Blob.objects.get_or_create(sha256="b" * 64, defaults={"size_bytes": 1})[0]
    co, day = company(), datetime.date.today()
    for n in range(start, start + reports):
        emp = make_user(Role.EMPLOYEE, reports_to=mgr, login_id=f"e{n}", full_name=f"Person {n:03d}")
        task = make_task(mgr, emp, title=f"Job {n}")
        a = TaskAssignment.objects.get(task=task, user=emp)
        for k in range(per_person):
            f = File.objects.create(company=co, display_name=f"doc-{n}-{k}", owner=emp)
            v = FileVersion.objects.create(
                file=f, version_number=1, file_code=f"c-{n}-{k}-{uuid.uuid4().hex[:6]}.pdf",
                original_filename=f"doc-{n}-{k}.pdf", blob=blob, size_bytes=1, uploaded_by=emp,
                idempotency_key=uuid.uuid4(), uploaded_on=day,
            )  # fmt: skip
            TaskFile.objects.create(
                task=task, file=f, version=v, assignment=a, relation="SUBMISSION", linked_by=emp
            )


def day_url() -> str:
    return f"/files/tasks/d/{FileVersion.objects.order_by('-uploaded_on').first().uploaded_on:%Y-%m-%d}"


def queries(who: str) -> int:
    c = login(who)
    with CaptureQueriesContext(connection) as q:
        assert c.get(day_url()).status_code == 200
    return len(q)


def test_the_query_count_does_not_grow_with_the_team():
    mgr = base()
    add_people(mgr, 0, 2, 2)
    small = queries("mgr")
    add_people(mgr, 2, 48, 10)  # now 50 reports, about 500 versions
    assert FileVersion.objects.count() >= 484
    big = queries("mgr")
    assert big <= small + 2, f"{small} queries for 2 reports, {big} for 50"
    page = login("mgr").get(day_url()).content.decode()
    assert "Person 000" in page and "Person 049" in page
    assert queries("boss") <= small + 4
    assert "Person 0" not in login("mgr2").get(day_url()).content.decode()
