"""F-ID-6: nobody creates or edits a role above their own. Each escalation is refused (403) and recorded as
security.denied.

transaction=True: the denial is written on its own connection, so the row must be committed to be seen.
"""

import uuid

import pytest
from django.test import Client
from ots.accounts.models import Company, Role, User
from ots.audit.models import AuditLog

from tests.accounts.factories import make_user

pytestmark = pytest.mark.django_db(transaction=True)
PW = "correct horse battery"


@pytest.fixture
def people():
    Company.objects.get_or_create(
        code="DEFAULT", defaults={"name": "Default"}
    )  # a transactional DB starts empty
    sa = make_user(Role.SUPER_ADMIN, login_id="boss")
    return {
        "sa": sa,
        "hr": make_user(Role.HR, reports_to=sa, login_id="hrx"),
        "sub": (sub := make_user(Role.SUB_ADMIN, reports_to=sa, login_id="subx")),
        "emp": make_user(Role.EMPLOYEE, reports_to=sub, login_id="empx"),
    }


def login(login_id):
    c = Client()
    assert c.post("/login", {"login_id": login_id, "password": PW}).status_code == 302
    return c


def post(c, url, **data):
    return c.post(url, data, HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()))


def new_person(c, role, manager, n):
    return post(
        c, "/app/people/new", login_id=f"nw{n}", employee_id=f"NW{n}", full_name=f"New {n}", role=role,
        reports_to=str(manager.pk), password="a-long-enough-password", _form="person",
    )  # fmt: skip


def denied_rows():
    return AuditLog.objects.filter(action="security.denied")


def test_a_sub_admin_cannot_create_a_sub_admin_hr_or_super_admin(people):
    c = login("subx")
    for i, role in enumerate((Role.SUB_ADMIN, Role.HR, Role.SUPER_ADMIN)):
        assert new_person(c, role, people["sa"], i).status_code == 403
    assert not User.objects.filter(login_id__startswith="nw").exists()
    assert denied_rows().count() == 3


def test_a_sub_admin_cannot_create_an_employee_outside_their_own_team(people):
    assert new_person(login("subx"), Role.EMPLOYEE, people["sa"], 9).status_code in (
        302,
        200,
    )  # forced into own team
    made = User.objects.get(login_id="nw9")
    assert (
        made.reports_to_id == people["sub"].pk
    )  # whatever manager was posted, a Sub-Admin's person is theirs


def test_hr_cannot_create_hr_or_super_admin(people):
    c = login("hrx")
    for i, role in enumerate((Role.HR, Role.SUPER_ADMIN)):
        assert new_person(c, role, people["sa"], i).status_code == 403
    assert not User.objects.filter(login_id__startswith="nw").exists()
    assert denied_rows().count() == 2


def test_hr_cannot_touch_a_super_admin_account(people):
    c = login("hrx")
    sa = people["sa"]
    assert post(c, f"/app/people/{sa.pk}/update", designation="x", version=sa.version).status_code == 403
    assert post(c, f"/app/people/{sa.pk}/deactivate", version=sa.version).status_code == 403
    assert post(c, f"/app/people/{sa.pk}/reset-password", new_password="x" * 14).status_code == 403
    assert User.objects.get(pk=sa.pk).is_active and User.objects.get(pk=sa.pk).designation == ""
    assert denied_rows().count() == 3


def test_hr_cannot_make_somebody_hr_or_touch_another_hr(people):
    c = login("hrx")
    emp = people["emp"]
    assert post(c, f"/app/people/{emp.pk}/update", role="HR", reports_to=str(people["sa"].pk),
                version=emp.version).status_code == 403  # fmt: skip
    assert User.objects.get(pk=emp.pk).role == Role.EMPLOYEE
    other = make_user(Role.HR, reports_to=people["sa"], login_id="hry")
    assert post(c, f"/app/people/{other.pk}/deactivate", version=other.version).status_code == 403
    assert denied_rows().count() == 2


def test_every_denial_names_the_person_and_the_permission(people):
    new_person(login("subx"), Role.SUB_ADMIN, people["sa"], 1)
    row = denied_rows().get()
    assert row.actor_id == people["sub"].pk and row.payload["permission"] == "user.create"
    assert row.payload["path"] == "/app/people/new"
