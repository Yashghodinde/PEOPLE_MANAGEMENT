import datetime as dt
import uuid

import pytest
from django.test import Client
from freezegun import freeze_time
from ots.core import clock, errors, permissions
from ots.core.ids import uuid7

pytestmark = pytest.mark.urls("tests.core.urls_test")


def test_uuid7_is_uuid_and_time_ordered():
    ids = [uuid7() for _ in range(50)]
    assert all(isinstance(i, uuid.UUID) and i.version == 7 for i in ids)
    assert ids == sorted(ids)


def test_clock_is_utc_aware_and_today_is_ist():
    with freeze_time("2026-11-09 20:00:00"):  # 01:30 next day in IST
        assert clock.now().tzinfo is not None
        assert clock.now().utcoffset() == dt.timedelta(0)
        assert clock.today_local() == dt.date(2026, 11, 10)


@pytest.mark.parametrize(
    "path,status,code",
    [
        ("403", 403, "permission_denied"),
        ("404", 404, "not_found"),
        ("409", 409, "conflict"),
        ("409t", 409, "invalid_transition"),
        ("422", 422, "validation_failed"),
        ("423", 423, "locked"),
        ("503", 503, "storage_unavailable"),
        ("507", 507, "quota_blocked"),
    ],
)
def test_error_mapping_json(path, status, code):
    r = Client().get(f"/e/{path}", HTTP_ACCEPT="application/json")
    assert r.status_code == status and r.json()["error"] == code


def test_error_payloads_carry_context():
    c = Client()
    assert c.get("/e/409", HTTP_HX_REQUEST="true").json()["current"] == {"v": 3}
    assert c.get("/e/422", HTTP_HX_REQUEST="true").json()["fields"] == {"title": "required"}


def test_full_page_error_is_html_and_escaped():
    r = Client().get("/e/xss")
    assert r.status_code == 404 and r["Content-Type"].startswith("text/html")
    assert b"<script>" not in r.content and b"&lt;script&gt;" in r.content
    assert b"Traceback" not in r.content


def test_request_id_generated_and_echoed_and_in_log_context():
    r = Client().get("/rid")
    assert r["X-Request-Id"] and r.content.decode() == r["X-Request-Id"]


def test_request_id_accepted_when_safe_replaced_when_not():
    c = Client()
    assert c.get("/rid", HTTP_X_REQUEST_ID="abc-123")["X-Request-Id"] == "abc-123"
    bad = c.get("/rid", HTTP_X_REQUEST_ID="x\ninjected")["X-Request-Id"]
    assert bad != "x\ninjected" and "\n" not in bad


def test_unknown_policy_fails_closed():
    with pytest.raises(errors.PermissionDenied):
        permissions.require(object(), "does.not.exist")
    assert permissions.can(object(), "does.not.exist") is False


def test_policy_allow_and_deny():
    @permissions.policy("test.allow_admin")
    def _p(user, obj):
        return getattr(user, "role", None) == "ADMIN"

    class U:
        def __init__(self, role):
            self.role = role

    permissions.require(U("ADMIN"), "test.allow_admin")
    with pytest.raises(errors.PermissionDenied):
        permissions.require(U("EMPLOYEE"), "test.allow_admin")


def test_duplicate_policy_registration_rejected():
    @permissions.policy("test.dup")
    def _a(u, o):
        return True

    with pytest.raises(RuntimeError):

        @permissions.policy("test.dup")
        def _b(u, o):
            return True
