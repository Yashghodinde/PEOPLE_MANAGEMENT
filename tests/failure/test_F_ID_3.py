"""F-ID-3: an Employee POSTing role=SUPER_ADMIN to the user-update endpoint gets 403 and a security.denied row.

transaction=True: the denial is written on its own connection, so the row must be committed to be seen.
"""

import uuid

import pytest
from django.test import Client
from ots.accounts.models import Role, User
from ots.audit.models import AuditLog

from tests.accounts.factories import make_user

pytestmark = pytest.mark.django_db(transaction=True)
PW = "correct horse battery"


def test_employee_cannot_escalate_role():
    sa = make_user(Role.SUPER_ADMIN)
    emp = make_user(Role.EMPLOYEE, reports_to=sa, login_id="mallory")
    c = Client()
    assert c.post("/login", {"login_id": "mallory", "password": PW}).status_code == 302
    r = c.post(
        f"/app/people/{emp.pk}/update",
        {"role": "SUPER_ADMIN", "version": emp.version},
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
    )
    assert r.status_code == 403
    assert User.objects.get(pk=emp.pk).role == Role.EMPLOYEE
    rows = AuditLog.objects.filter(action="security.denied")
    assert rows.count() == 1
    row = rows.get()
    assert row.actor_id == emp.pk and row.payload["permission"] == "user.update"
    assert row.payload["path"] == f"/app/people/{emp.pk}/update"


def test_super_admin_can_update_via_same_endpoint():
    sa = make_user(Role.SUPER_ADMIN, login_id="boss")
    emp = make_user(Role.EMPLOYEE, reports_to=sa)
    c = Client()
    c.post("/login", {"login_id": "boss", "password": PW})
    r = c.post(
        f"/app/people/{emp.pk}/update",
        {"designation": "Clerk", "version": emp.version},
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
    )
    assert r.status_code == 302
    assert User.objects.get(pk=emp.pk).designation == "Clerk"
    assert not AuditLog.objects.filter(action="security.denied").exists()
