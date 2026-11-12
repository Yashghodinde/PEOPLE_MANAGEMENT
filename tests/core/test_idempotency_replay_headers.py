"""A replayed answer repeats the headers that steer the browser: Location and the HTMX redirect/refresh."""

import uuid

import pytest
from django.test import Client
from ots.accounts.models import Role
from ots.core.models import IdempotencyRecord

from tests.accounts.factories import make_user

pytestmark = pytest.mark.django_db
PW = "correct horse battery"


@pytest.fixture
def setup():
    sa = make_user(Role.SUPER_ADMIN, login_id="boss")
    sub = make_user(Role.SUB_ADMIN, reports_to=sa, login_id="sub")
    emp = make_user(Role.EMPLOYEE, reports_to=sub, login_id="emp")
    c = Client()
    assert c.post("/login", {"login_id": "boss", "password": PW}).status_code == 302
    return c, emp


def edit(emp, **over):
    data = {"_form": "person", "version": emp.version, "full_name": emp.full_name, "employee_id": emp.employee_id,
            "role": emp.role, "reports_to": str(emp.reports_to_id), "weekly_off": ["6"],
            "shift_start": "09:30", "shift_end": "18:30", "designation": "Clerk"}  # fmt: skip
    data.update(over)
    return data


def test_an_htmx_double_submit_is_told_to_redirect_both_times(setup):
    c, emp = setup
    key = str(uuid.uuid4())
    first = c.post(
        f"/app/people/{emp.pk}/update", edit(emp), HTTP_IDEMPOTENCY_KEY=key, HTTP_HX_REQUEST="true"
    )
    again = c.post(
        f"/app/people/{emp.pk}/update", edit(emp), HTTP_IDEMPOTENCY_KEY=key, HTTP_HX_REQUEST="true"
    )
    assert first["HX-Redirect"] == again["HX-Redirect"] == f"/app/people/{emp.pk}"
    assert again["Idempotent-Replay"] == "true"


def test_a_plain_double_submit_is_redirected_both_times(setup):
    c, emp = setup
    key = str(uuid.uuid4())
    first = c.post(f"/app/people/{emp.pk}/update", edit(emp), HTTP_IDEMPOTENCY_KEY=key)
    again = c.post(f"/app/people/{emp.pk}/update", edit(emp), HTTP_IDEMPOTENCY_KEY=key)
    assert first.status_code == again.status_code == 302 and first["Location"] == again["Location"]


def test_only_the_steering_headers_are_kept(setup):
    c, emp = setup
    key = str(uuid.uuid4())
    c.post(f"/app/people/{emp.pk}/update", edit(emp), HTTP_IDEMPOTENCY_KEY=key)
    stored = IdempotencyRecord.objects.get(key=key).headers
    assert set(stored) == {"Location"}  # no cookies, no cache directives, nothing personal


def test_old_records_without_headers_still_replay(setup):
    c, emp = setup
    key = str(uuid.uuid4())
    c.post(f"/app/people/{emp.pk}/update", edit(emp), HTTP_IDEMPOTENCY_KEY=key)
    IdempotencyRecord.objects.filter(key=key).update(headers={})  # a record made before this fix
    again = c.post(f"/app/people/{emp.pk}/update", edit(emp), HTTP_IDEMPOTENCY_KEY=key)
    assert again.status_code == 302 and again["Idempotent-Replay"] == "true"
