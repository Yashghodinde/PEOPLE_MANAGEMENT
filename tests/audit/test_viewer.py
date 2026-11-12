"""M03.2: the /audit viewer, its CSV export and the profile Activity tab."""

import csv
import io
import re
import uuid

import pytest
from django.db import connection, transaction
from django.test import Client
from django.test.utils import CaptureQueriesContext
from freezegun import freeze_time
from ots.accounts.models import Role
from ots.audit import api as audit
from ots.audit.models import AuditLog

from tests.accounts.factories import make_user

pytestmark = pytest.mark.django_db
PW = "correct horse battery"


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


def put(action, actor=None, target=None, payload=None):
    with transaction.atomic():
        audit.record(action, actor, target, payload or {})


def actions_on(page_text):
    return set(re.findall(r"\b(?:auth|user|settings|file|security)\.[a-z_]+", page_text))


# --- who sees what (the M02 table) ---------------------------------------------------------------


def test_scoping_per_role(org):
    put("settings.changed", org["sa"], ("setting", "x"))  # Super Admin only: not about anyone below
    put("user.updated", org["sa"], org["emp"])  # about a Sub-Admin's own team member
    put("user.updated", org["sa"], org["emp2"])  # about someone else's team
    put("auth.login_failed", org["emp"], org["emp"])  # emp's own sign-in record
    put("auth.login_ok", org["emp2"], org["emp2"])

    boss = login("boss").get("/audit").content.decode()
    assert "settings.changed" in boss and boss.count("user.updated") == 2

    sub = login("sub").get("/audit").content.decode()
    assert "settings.changed" not in sub
    assert sub.count("user.updated") == 1  # only the one about emp (own team)
    assert "Ravi Other" not in sub

    emp = login("emp").get("/audit").content.decode()
    assert "user.updated" not in emp and "settings.changed" not in emp  # not a login record
    assert "auth.login_failed" in emp
    assert "Ravi" not in emp


def test_employee_sees_only_own_login_records(org):
    put("auth.login_ok", org["emp2"], org["emp2"])
    c = login("emp")
    own = AuditLog.objects.filter(action="auth.login_ok", actor_id=org["emp"].pk).count()
    assert own >= 1  # their own login just now
    body = c.get("/audit").content.decode()
    assert "Ravi" not in body
    assert str(org["emp2"].pk) not in body


def test_anonymous_is_sent_to_login():
    for path in ("/audit", "/audit/export"):
        r = Client().get(path)
        assert r.status_code == 302 and r["Location"].startswith("/login")


# --- filters ---------------------------------------------------------------------------------------


def test_filters_actor_action_target_and_dates(org):
    with freeze_time("2026-11-10 06:00:00"):
        put("user.created", org["sa"], org["emp"])
    with freeze_time("2026-11-12 06:00:00"):
        put("user.updated", org["sa"], org["emp"])
        put("settings.changed", org["sub"], ("setting", "idle"))
    c = login("boss")
    assert actions_on(c.get("/audit?action=user.").content.decode()) >= {"user.created", "user.updated"}
    body = c.get("/audit?action=user.&from=2026-11-12&to=2026-11-12").content.decode()
    assert "user.updated" in body and "user.created" not in body and "settings.changed" not in body
    body = c.get(f"/audit?actor={org['sub'].pk}&action=settings.").content.decode()
    assert "settings.changed" in body and "user.updated" not in body
    body = c.get(f"/audit?target={org['emp'].pk}&action=user.").content.decode()
    assert "user.created" in body and "settings.changed" not in body


@pytest.mark.parametrize("qs", ["actor=abc", "from=nope", "to=2026-13-45", "cursor=garbage", "target=%00"])
def test_bad_filter_values_never_cause_a_500(org, qs):
    r = login("boss").get(f"/audit?{qs}")
    assert r.status_code in (200, 422)


# --- paging: 100 per page, nothing skipped ----------------------------------------------------------


def test_pages_are_100_rows_and_cover_everything_once(org):
    with transaction.atomic():
        for i in range(230):
            audit.record("settings.changed", org["sa"], ("setting", f"k{i:03d}"))
    c = login("boss")
    seen, url, pages = [], "/audit?action=settings.", 0
    while url:
        body = c.get(url).content.decode()
        seen += re.findall(r"setting k(\d{3})", body)
        m = re.search(r'href="(\?[^"]*cursor=[^"]*)"', body)
        url = "/audit" + m.group(1).replace("&amp;", "&") if m else None
        pages += 1
        assert pages < 10
    assert pages == 3 and sorted(seen) == [f"{i:03d}" for i in range(230)]


def test_page_query_count_does_not_grow_with_rows(org):
    with transaction.atomic():
        for i in range(100):
            audit.record("settings.changed", org["sa"], ("setting", f"q{i}"))
    c = login("boss")
    with CaptureQueriesContext(connection) as ctx:
        c.get("/audit?action=settings.")
    assert len(ctx) <= 8


# --- CSV export ---------------------------------------------------------------------------------


def _csv(response):
    assert response.streaming
    return list(csv.reader(io.StringIO(b"".join(response.streaming_content).decode())))


def test_export_streams_the_filtered_result_and_is_audited(org):
    put("settings.changed", org["sa"], ("setting", "a"))
    put("user.updated", org["sa"], org["emp"])
    r = login("boss").get("/audit/export?action=settings.")
    assert r["Content-Type"].startswith("text/csv") and "attachment" in r["Content-Disposition"]
    rows = _csv(r)
    assert rows[0][:5] == ["id", "time_ist", "actor_id", "actor", "action"]
    assert {row[4] for row in rows[1:]} == {"settings.changed"}
    exported = AuditLog.objects.get(action="audit.exported")
    assert exported.actor_id == org["sa"].pk and exported.payload == {"action": "settings."}


def test_export_has_the_same_scope_as_the_screen(org):
    put("user.updated", org["sa"], org["emp"])
    put("user.updated", org["sa"], org["emp2"])
    rows = _csv(login("sub").get("/audit/export?action=user."))
    targets = {row[6] for row in rows[1:]}
    assert targets == {str(org["emp"].pk)}


def test_export_neutralises_spreadsheet_formulas(org):
    put("auth.login_failed", None, None, {"login_id": '=HYPERLINK("http://evil","x")', "reason": "unknown"})
    rows = _csv(login("boss").get("/audit/export?action=auth.login_failed"))
    details = [row[9] for row in rows[1:] if "HYPERLINK" in row[9]]
    assert details and not details[0].startswith("=")
    for row in rows[1:]:
        assert not row[4].startswith(("=", "+", "@"))


def test_export_does_not_load_everything_into_memory(org):
    with transaction.atomic():
        for i in range(1200):
            audit.record("settings.changed", org["sa"], ("setting", f"m{i}"))
    r = login("boss").get("/audit/export?action=settings.")
    first = next(iter(r.streaming_content))  # the first chunk arrives before the rest is read
    assert first.startswith(b"id,time_ist")
    assert len(_csv(login("boss").get("/audit/export?action=settings."))) == 1201


# --- profile Activity tab -------------------------------------------------------------------------


def test_activity_endpoint_is_scoped_like_the_profile(org):
    put("auth.login_ok", org["emp"], org["emp"])
    assert login("boss").get(f"/app/people/{org['emp'].pk}/activity").status_code == 200
    assert login("sub").get(f"/app/people/{org['emp'].pk}/activity").status_code == 200
    assert login("emp").get(f"/app/people/{org['emp'].pk}/activity").status_code == 200  # themselves
    assert login("sub").get(f"/app/people/{org['emp2'].pk}/activity").status_code == 404
    assert login("emp").get(f"/app/people/{org['sub'].pk}/activity").status_code == 404
    assert login("boss").get(f"/app/people/{uuid.uuid4()}/activity").status_code == 404


def test_activity_shows_sign_ins_and_the_profile_loads_it(org):
    c = login("boss")
    body = c.get(f"/app/people/{org['emp'].pk}").content.decode()
    assert f'hx-get="/app/people/{org["emp"].pk}/activity"' in body and 'hx-trigger="load"' in body
    login("emp")  # a fresh sign-in by the employee
    html = c.get(f"/app/people/{org['emp'].pk}/activity").content.decode()
    assert "Signed in" in html and "<html" not in html


def test_activity_empty_state_and_bad_dates(org):
    html = (
        login("boss")
        .get(f"/app/people/{org['sub2'].pk}/activity?from=2020-01-01&to=2020-01-02")
        .content.decode()
    )
    assert "No sign-in activity" in html
    r = login("boss").get(f"/app/people/{org['emp'].pk}/activity?from=junk&cursor=junk")
    assert r.status_code == 422


def test_audit_link_is_in_the_navigation_for_everyone(org):
    assert 'href="/audit"' in login("boss").get("/app/people").content.decode()
    assert 'href="/audit"' in login("sub").get("/app/people").content.decode()
    assert 'href="/audit"' in login("emp").get(f"/app/people/{org['emp'].pk}").content.decode()


def test_control_characters_in_filters_are_dropped_not_a_500(org):
    put("settings.changed", org["sa"], ("setting", "z"))
    c = login("boss")
    assert c.get("/audit?action=%00settings.&target=%00").status_code == 200
    assert "settings.changed" in c.get("/audit?action=%00settings.%07").content.decode()
    assert c.get("/audit/export?action=%00settings.").status_code == 200


def test_about_column_names_the_person(org):
    put("user.updated", org["sa"], org["emp"])
    put("settings.changed", org["sa"], ("setting", "idle"))
    body = login("boss").get("/audit").content.decode()
    assert "Asha Employee (emp)" in body and "setting idle" in body
    assert f"user {org['emp'].pk}" not in body
