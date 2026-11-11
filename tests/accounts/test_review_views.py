"""Regression tests for the M02 code-review findings on the people screens."""

import re
import uuid

import pytest
from django.test import Client
from ots.accounts.models import Role, User
from ots.audit.models import AuditLog

from tests.accounts.factories import make_user

pytestmark = pytest.mark.django_db
PW = "correct horse battery"


def key():
    return {"HTTP_IDEMPOTENCY_KEY": str(uuid.uuid4())}


def login(login_id):
    c = Client()
    assert c.post("/login", {"login_id": login_id, "password": PW}).status_code == 302
    return c


@pytest.fixture
def org():
    sa = make_user(Role.SUPER_ADMIN, login_id="boss", full_name="Boss Person")
    sub = make_user(Role.SUB_ADMIN, reports_to=sa, login_id="sub", full_name="Sub Admin")
    sub2 = make_user(Role.SUB_ADMIN, reports_to=sa, login_id="sub2", full_name="Other Sub")
    emp = make_user(Role.EMPLOYEE, reports_to=sub, login_id="emp", full_name="Asha Employee")
    outsider = make_user(
        Role.EMPLOYEE,
        reports_to=sub2,
        login_id="emp2",
        full_name="Ravi Other",
        designation="SECRET-DESIGNATION",
    )
    return {"sa": sa, "sub": sub, "sub2": sub2, "emp": emp, "outsider": outsider}


def form(emp, **over):
    data = {
        "_form": "person", "version": emp.version, "full_name": emp.full_name, "employee_id": emp.employee_id,
        "role": emp.role, "reports_to": str(emp.reports_to_id), "weekly_off": ["6"],
        "shift_start": "09:30", "shift_end": "18:30",
    }  # fmt: skip
    data.update(over)
    return data


# --- P1 / P2: the version is required and must be a number -------------------------------------


def test_update_without_a_version_is_refused_and_changes_nothing(org):
    emp = org["emp"]
    data = form(emp, designation="Sneaky")
    del data["version"]
    r = login("boss").post(f"/app/people/{emp.pk}/update", data, **key())
    assert r.status_code == 422 and "out of date" in r.content.decode()
    assert User.objects.get(pk=emp.pk).designation == ""


def test_update_with_a_garbage_version_is_a_422_not_a_500(org):
    emp = org["emp"]
    r = login("boss").post(f"/app/people/{emp.pk}/update", form(emp, version="abc"), **key())
    assert r.status_code == 422


# --- P3: bad ids in the URL or form never cause a 500 -----------------------------------------


@pytest.mark.parametrize("qs", ["department=abc", "manager=abc", "manager=%00", "department=none&manager=1"])
def test_list_filters_ignore_bad_ids(org, qs):
    r = login("boss").get(f"/app/people?{qs}")
    assert r.status_code == 200


def test_bad_manager_or_department_in_a_post_is_a_field_error(org):
    emp = org["emp"]
    c = login("boss")
    r = c.post(f"/app/people/{emp.pk}/update", form(emp, reports_to="not-a-uuid"), **key())
    assert r.status_code == 422 and "valid manager" in r.content.decode()
    r = c.post(f"/app/people/{emp.pk}/update", form(emp, department="not-a-uuid"), **key())
    assert r.status_code == 422 and "Unknown department" in r.content.decode()
    r = c.post("/app/people/new", form(emp, login_id="n1", password="x" * 12, reports_to="zzz"), **key())
    assert r.status_code == 422


# --- V1: an HTMX error re-render is only the form -----------------------------------------------


def test_htmx_validation_error_returns_only_the_form(org):
    emp = org["emp"]
    r = login("boss").post(
        f"/app/people/{emp.pk}/update",
        form(emp, date_of_joining="not-a-date"),
        HTTP_HX_REQUEST="true",
        **key(),
    )
    body = r.content.decode()
    assert r.status_code == 422
    assert "<html" not in body and body.count('id="person-form"') == 1
    assert "Invalid value." in body


def test_full_page_error_still_renders_the_whole_page(org):
    emp = org["emp"]
    r = login("boss").post(f"/app/people/{emp.pk}/update", form(emp, date_of_joining="not-a-date"), **key())
    assert r.status_code == 422 and "<html" in r.content.decode()


# --- V4 / V5: odd weekly-off values and blank shift times ----------------------------------------


def test_odd_weekly_off_values_are_ignored(org):
    emp = org["emp"]
    r = login("boss").post(
        f"/app/people/{emp.pk}/update", form(emp, weekly_off=["²", "٣", "9", "-1", "5", "5"]), **key()
    )
    assert r.status_code == 302
    assert User.objects.get(pk=emp.pk).weekly_off == [5]


def test_blank_shift_time_clears_the_shift(org):
    emp = org["emp"]
    r = login("boss").post(f"/app/people/{emp.pk}/update", form(emp, shift_start="", shift_end=""), **key())
    assert r.status_code == 302
    emp.refresh_from_db()
    assert emp.shift_start is None and emp.shift_end is None
    page = login("boss").get(
        f"/app/people/{emp.pk}/edit"
    )  # the edit form still opens for a person with no shift
    assert page.status_code == 200


# --- V6: next/previous keep the filters ---------------------------------------------------------


def test_pagination_links_keep_filters(org):
    for _ in range(55):
        make_user(Role.EMPLOYEE, reports_to=org["sub"])
    body = login("boss").get("/app/people?role=EMPLOYEE&active=1").content.decode()
    nxt = re.search(r'href="(\?[^"]*page=2[^"]*)"', body)
    assert nxt and "role=EMPLOYEE" in nxt.group(1) and "active=1" in nxt.group(1)
    page2 = login("boss").get("/app/people" + nxt.group(1).replace("&amp;", "&"))
    assert page2.status_code == 200


# --- V7: forms still work without JavaScript (plain post, no Idempotency-Key) ----------------


def test_plain_form_post_works_without_a_key(org):
    emp = org["emp"]
    c = login("boss")
    body = c.get(f"/app/people/{emp.pk}/edit").content.decode()
    tag = re.search(r'<form id="person-form"[^>]*>', body, re.S).group(0)
    assert 'method="post"' in tag and f'action="/app/people/{emp.pk}/update"' in tag and "hx-post" in tag
    r = c.post(f"/app/people/{emp.pk}/update", form(emp, designation="No JS"))  # no Idempotency-Key header
    assert r.status_code == 302 and r["Location"] == f"/app/people/{emp.pk}"
    assert User.objects.get(pk=emp.pk).designation == "No JS"
    bad = c.post(f"/app/people/{emp.pk}/update", form(emp, date_of_joining="nope"))
    assert bad.status_code == 422 and "<html" in bad.content.decode()


def test_a_key_that_is_sent_is_still_enforced_and_replayed(org):
    emp = org["emp"]
    c = login("boss")
    k = key()
    first = c.post(f"/app/people/{emp.pk}/update", form(emp, designation="Once"), **k)
    again = c.post(f"/app/people/{emp.pk}/update", form(emp, designation="Twice"), **k)
    assert first.status_code == again.status_code == 302
    assert User.objects.get(pk=emp.pk).designation == "Once"  # the replay did not run the change again
    assert (
        c.post(f"/app/people/{emp.pk}/update", form(emp), HTTP_IDEMPOTENCY_KEY="not-a-uuid").status_code
        == 400
    )


def test_other_data_changing_views_still_require_a_key(org):
    c = login("boss")
    assert c.post(f"/app/people/{org['emp'].pk}/deactivate").status_code == 428


# --- P4: the org chart hands outsiders' rows to the template as name + id only -------------------


def test_sub_admin_org_chart_is_own_team_only(org):
    c = login("sub")
    body = c.get("/app/org").content.decode()
    assert "Sub Admin" in body and "Boss Person" not in body and "Other Sub" not in body
    assert "Ravi Other" not in body and "SECRET-DESIGNATION" not in body
    assert "Asha Employee" in c.get(f"/app/org/children/{org['sub'].pk}").content.decode()
    for outside in ("sa", "sub2", "outsider"):  # anyone outside the team answers like a missing person
        assert c.get(f"/app/org/children/{org[outside].pk}").status_code == 404


def test_org_children_of_unknown_or_break_glass_is_404(org):
    bg = make_user(Role.SUPER_ADMIN, login_id="breakglass", is_break_glass=True)
    c = login("boss")
    assert c.get(f"/app/org/children/{uuid.uuid4()}").status_code == 404
    assert c.get(f"/app/org/children/{bg.pk}").status_code == 404
    assert "breakglass" not in c.get("/app/org").content.decode().lower()


# --- P5: admin row actions look people up through the scoped manager ----------------------------


def test_admin_actions_on_a_missing_person_are_404(org):
    c = login("boss")
    ghost = uuid.uuid4()
    for action in ("deactivate", "reactivate", "reset-password", "update"):
        assert c.post(f"/app/people/{ghost}/{action}", form(org["emp"]), **key()).status_code == 404


def test_row_actions_check_a_supplied_version(org):
    c = login("boss")
    emp = org["emp"]
    r = c.post(f"/app/people/{emp.pk}/deactivate", {"version": emp.version + 3}, **key())
    assert r.status_code == 409
    assert User.objects.get(pk=emp.pk).is_active
    r = c.post(f"/app/people/{emp.pk}/deactivate", {"version": "abc"}, **key())
    assert r.status_code == 422


# --- the security.denied audit is still written for a forbidden edit ---------------------------


def test_forbidden_edit_is_still_audited(org):
    emp = org["emp"]
    r = login("emp").post(f"/app/people/{emp.pk}/update", form(emp, role="SUPER_ADMIN"), **key())
    assert r.status_code == 403
    assert AuditLog.objects.filter(action="security.denied").exists()


def test_profile_actions_send_the_version_they_were_rendered_with(org):
    emp = org["emp"]
    body = login("boss").get(f"/app/people/{emp.pk}").content.decode()
    assert f"&quot;version&quot;: {emp.version}" in body or f'"version": {emp.version}' in body
    assert f'name="version" value="{emp.version}"' in body


def test_error_rerender_keeps_times_dates_and_department_in_input_format(org):
    from ots.accounts.models import Department

    emp = org["emp"]
    dept = Department.objects.create(company=emp.company, name="Operations")
    r = login("boss").post(
        f"/app/people/{emp.pk}/update",
        form(emp, employee_id=org["sa"].employee_id, department=str(dept.pk), date_of_joining="2026-03-04",
             shift_start="09:30", shift_end="18:45"),
        **key(),
    )  # fmt: skip
    body = r.content.decode()
    assert r.status_code == 422
    assert 'value="09:30"' in body and 'value="18:45"' in body and 'value="2026-03-04"' in body
    assert re.search(rf'<option value="{dept.pk}" selected', body)
    assert "a.m." not in body and "p.m." not in body
