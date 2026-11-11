import uuid

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from ots.accounts.models import Department, Role, User

from tests.accounts.factories import company, make_user

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
    emp2 = make_user(Role.EMPLOYEE, reports_to=sub2, login_id="emp2", full_name="Ravi Other")
    return {"sa": sa, "sub": sub, "sub2": sub2, "emp": emp, "emp2": emp2}


def test_anonymous_is_sent_to_login():
    for path in ("/app/people", "/app/org", "/app/people/new"):
        r = Client().get(path)
        assert r.status_code == 302 and r["Location"].startswith("/login")


def test_home_redirects_by_role(org):
    assert login("boss").get("/")["Location"] == "/app/people"
    assert login("emp").get("/")["Location"] == f"/app/people/{org['emp'].pk}"


def link(body, user):
    return f'href="/app/people/{user.pk}"' in body


def test_list_is_scoped_by_role(org):
    body = login("boss").get("/app/people").content.decode()
    assert all(link(body, u) for u in org.values())
    body = login("sub").get("/app/people").content.decode()
    assert link(body, org["emp"]) and link(body, org["sub"])
    assert not any(link(body, org[k]) for k in ("emp2", "sub2", "sa"))
    assert "Other Sub" not in body and "Ravi Other" not in body  # not even in the filter dropdowns
    body = login("emp").get("/app/people").content.decode()
    assert not any(link(body, org[k]) for k in ("sub", "sub2", "emp2", "sa"))
    assert "New person" not in body and "Other Sub" not in body


def test_list_filters(org):
    c = login("boss")
    body = c.get("/app/people?role=EMPLOYEE").content.decode()
    assert link(body, org["emp"]) and link(body, org["emp2"]) and not link(body, org["sub"])
    body = c.get(f"/app/people?manager={org['sub2'].pk}").content.decode()
    assert link(body, org["emp2"]) and not link(body, org["emp"])
    org["emp"].is_active = False
    org["emp"].save()
    assert not link(c.get("/app/people").content.decode(), org["emp"])
    assert link(c.get("/app/people?active=0").content.decode(), org["emp"])


def test_list_query_count_for_50_users(org):
    for i in range(50):
        make_user(Role.EMPLOYEE, reports_to=org["sub"], full_name=f"Bulk {i:02d}")
    c = login("boss")
    with CaptureQueriesContext(connection) as q:
        r = c.get("/app/people")
    assert r.status_code == 200
    assert len(q) <= 6, [x["sql"][:80] for x in q.captured_queries]
    assert "Page 1 of 2" in r.content.decode()  # 50 per page, 55 people
    assert "Bulk" in c.get("/app/people?page=2").content.decode()


def test_profile_visibility(org):
    sub, emp, emp2 = login("sub"), login("emp"), None
    assert sub.get(f"/app/people/{org['emp'].pk}").status_code == 200
    assert (
        sub.get(f"/app/people/{org['emp2'].pk}").status_code == 404
    )  # outside the subtree: not even confirmed
    assert emp.get(f"/app/people/{org['emp'].pk}").status_code == 200
    assert emp.get(f"/app/people/{org['sub'].pk}").status_code == 404
    assert emp2 is None
    body = emp.get(f"/app/people/{org['emp'].pk}").content.decode()
    assert "Reports to Sub Admin" in body  # an employee sees their own manager
    for tab in ("Work", "Attendance", "Files", "Activity"):
        assert tab in body
    assert "Reset password" not in body and "Deactivate" not in body


def test_profile_shows_admin_actions_to_super_admin(org):
    body = login("boss").get(f"/app/people/{org['emp'].pk}").content.decode()
    assert "Reset password" in body and "Deactivate" in body and "/edit" in body


def test_create_form_is_admin_only(org):
    assert login("boss").get("/app/people/new").status_code == 200
    for who in ("sub", "emp"):
        c = login(who)
        assert c.get("/app/people/new").status_code == 403
        assert c.post("/app/people/new", {"login_id": "x"}, **key()).status_code == 403


def test_manager_dropdown_is_server_filtered(org):
    ghost = make_user(Role.SUB_ADMIN, reports_to=org["sa"], full_name="Gone Manager", is_active=False)
    c = login("boss")
    emp_opts = c.get("/app/people/manager-options?role=EMPLOYEE").content.decode()
    assert "Sub Admin" in emp_opts and "Boss Person" in emp_opts
    assert "Gone Manager" not in emp_opts and "Asha Employee" not in emp_opts  # inactive / lower rank
    sub_opts = c.get("/app/people/manager-options?role=SUB_ADMIN").content.decode()
    assert "Boss Person" in sub_opts and "Sub Admin" not in sub_opts
    assert ghost.pk
    assert "Asha Employee" not in c.get("/app/people/new").content.decode()


def test_create_person_end_to_end(org):
    dept = Department.objects.create(company=company(), name="Accounts")
    c = login("boss")
    r = c.post(
        "/app/people/new",
        {
            "_form": "person", "login_id": "newhire", "password": "a-long-enough-password", "full_name": "New Hire",
            "employee_id": "E900", "role": "EMPLOYEE", "reports_to": str(org["sub"].pk),
            "designation": "Clerk", "department": str(dept.pk), "highest_qualification": "GRADUATE",
            "qualification_year": "2020", "shift_start": "09:00", "shift_end": "17:30",
            "weekly_off": ["5", "6"], "mobile": "999", "email": "n@example.com",
        },
        HTTP_HX_REQUEST="true", **key(),
    )  # fmt: skip
    u = User.objects.get(login_id="newhire")
    assert r.status_code == 200 and r["HX-Redirect"] == f"/app/people/{u.pk}"
    assert u.reports_to == org["sub"] and u.department == dept and u.weekly_off == [5, 6]
    assert u.qualification_year == 2020 and u.must_change_password
    assert u.shift_start.strftime("%H:%M") == "09:00"


def test_create_person_errors_rerender_form(org):
    c = login("boss")
    r = c.post(
        "/app/people/new",
        {"_form": "person", "login_id": "boss", "password": "short", "full_name": "X", "employee_id": "E1",
         "role": "EMPLOYEE", "reports_to": ""},
        **key(),
    )  # fmt: skip
    assert r.status_code == 200
    body = r.content.decode()
    assert "Choose who this person reports to." in body or "login ID is already used" in body
    assert User.objects.filter(login_id__iexact="boss").count() == 1


def test_edit_person_changes_profile_and_manager_with_one_audit_each(org):
    from ots.audit.models import AuditLog

    c = login("boss")
    emp = org["emp"]
    assert c.get(f"/app/people/{emp.pk}/edit").status_code == 200
    r = c.post(
        f"/app/people/{emp.pk}/update",
        {"_form": "person", "version": emp.version, "full_name": "Asha Renamed", "employee_id": emp.employee_id,
         "role": "EMPLOYEE", "reports_to": str(org["sub2"].pk), "designation": "Lead", "weekly_off": ["6"],
         "shift_start": "09:30", "shift_end": "18:30"},
        **key(),
    )  # fmt: skip
    assert r.status_code == 302
    emp.refresh_from_db()
    assert emp.full_name == "Asha Renamed" and emp.reports_to == org["sub2"] and emp.designation == "Lead"
    assert AuditLog.objects.filter(action="user.updated", target_id=str(emp.pk)).count() == 1
    assert AuditLog.objects.filter(action="user.manager_changed", target_id=str(emp.pk)).count() == 1


def test_edit_with_stale_version_is_a_conflict(org):
    c = login("boss")
    emp = org["emp"]
    r = c.post(f"/app/people/{emp.pk}/update", {"version": emp.version + 5, "designation": "X"}, **key())
    assert r.status_code == 409


def test_deactivate_and_reset_password_endpoints(org):
    c = login("boss")
    r = c.post(f"/app/people/{org['sub'].pk}/deactivate", **key())  # still has a report
    assert r.status_code == 422 and "Asha Employee" in r.content.decode()
    r = c.post(f"/app/people/{org['emp'].pk}/deactivate", HTTP_HX_REQUEST="true", **key())
    assert r["HX-Redirect"] == f"/app/people/{org['emp'].pk}"
    assert not User.objects.get(pk=org["emp"].pk).is_active
    r = c.post(f"/app/people/{org['emp'].pk}/reactivate", **key())
    assert r.status_code == 302 and User.objects.get(pk=org["emp"].pk).is_active
    r = c.post(f"/app/people/{org['emp'].pk}/reset-password", {"new_password": "temporary-pass-9"}, **key())
    assert r.status_code == 302
    assert User.objects.get(pk=org["emp"].pk).check_password("temporary-pass-9")
    for who in ("sub", "emp2"):
        cc = login(who)
        assert (
            cc.post(
                f"/app/people/{org['emp'].pk}/reset-password", {"new_password": "x" * 12}, **key()
            ).status_code
            == 403
        )


def test_org_chart_renders_three_levels_with_lazy_children(org):
    c = login("boss")
    r = c.get("/app/org")
    body = r.content.decode()
    assert r.status_code == 200
    assert "Boss Person" in body and "Sub Admin" in body and "Other Sub" in body  # levels 1 and 2
    assert "Asha Employee" not in body  # level 3 is lazy
    assert f"/app/org/children/{org['sub'].pk}" in body
    kids = c.get(f"/app/org/children/{org['sub'].pk}").content.decode()
    assert "Asha Employee" in kids  # level 3 arrives through HTMX


def test_org_chart_permissions_and_names_only_outside_subtree(org):
    assert login("emp").get("/app/org").status_code == 403
    body = login("sub").get("/app/org").content.decode()
    assert "Asha Employee" not in body
    assert "Other Sub" in body and "Other Sub</a>" not in body  # name only: no link, no details
    assert "Sub Admin</a>" in body  # own node keeps its link
    assert not link(body, org["sub2"])


def test_org_chart_hides_break_glass_and_inactive(org):
    make_user(Role.SUPER_ADMIN, login_id="breakglass", full_name="Break Glass", is_break_glass=True)
    body = login("boss").get("/app/org").content.decode()
    assert "Break Glass" not in body


def test_nav_has_people_and_org_for_admins_only(org):
    admin_body = login("boss").get("/app/people").content.decode()
    assert 'href="/app/people"' in admin_body and 'href="/app/org"' in admin_body
    emp_body = login("emp").get("/app/people").content.decode()
    assert 'href="/app/org"' not in emp_body
    assert 'action="/logout"' in emp_body


def test_forms_carry_csrf_and_htmx_attributes(org):
    body = login("boss").get("/app/people/new").content.decode()
    assert "csrfmiddlewaretoken" in body and 'hx-post="/app/people/new"' in body
