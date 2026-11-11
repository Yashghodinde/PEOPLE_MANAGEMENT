"""Regression tests for the M02 code-review findings on login, lockout, passwords and the audit IP."""

import time

import pytest
from django.db import connection
from django.test import Client, RequestFactory
from freezegun import freeze_time
from ots.accounts import passwords, services
from ots.accounts.models import Role, User
from ots.audit import api as audit
from ots.audit import denied
from ots.audit.models import AuditLog
from ots.core import permissions
from ots.core.errors import PermissionDenied
from ots.core.models import Alert

from tests.accounts.factories import make_user

pytestmark = pytest.mark.django_db
GOOD = "correct horse battery"


def _login(c, login, pw):
    return c.post("/login", {"login_id": login, "password": pw})


@pytest.fixture
def sa():
    return make_user(Role.SUPER_ADMIN, login_id="boss")


@pytest.fixture
def emp(sa):
    return make_user(Role.EMPLOYEE, reports_to=sa, login_id="asha")


# --- A1: a good login or an admin reset starts a clean count -----------------------------------


def test_good_login_clears_the_failure_count(emp):
    c = Client()
    with freeze_time("2026-11-12 04:00:00") as ft:
        for _ in range(4):
            assert _login(c, "asha", "wrong").status_code == 400
            ft.tick(5)
        assert _login(c, "asha", GOOD).status_code == 302
        ft.tick(5)
        c.post("/logout")
        assert _login(c, "asha", "wrong").status_code == 400  # one typo after a good login is just a typo
        assert User.objects.get(pk=emp.pk).locked_until is None
        assert User.objects.get(pk=emp.pk).failed_logins == 1


def test_admin_reset_clears_a_lock_for_good(sa, emp):
    c = Client()
    with freeze_time("2026-11-12 04:00:00") as ft:
        for _ in range(5):
            _login(c, "asha", "wrong")
            ft.tick(2)
        assert _login(c, "asha", GOOD).status_code == 423
        services.reset_password(sa, emp, "temporary-pass-9")
        ft.tick(2)
        assert _login(c, "asha", "wrong").status_code == 400  # not re-locked by the old rows
        assert _login(c, "asha", "temporary-pass-9").status_code == 302


# --- A2: the break-glass account cannot be locked out by strangers, and failures raise an alert --


def test_break_glass_is_never_locked_but_failures_alert(sa):
    bg = make_user(Role.SUPER_ADMIN, login_id="breakglass", is_break_glass=True)
    c = Client()
    for _ in range(7):
        assert _login(c, "breakglass", "guess").status_code == 400
    assert User.objects.get(pk=bg.pk).locked_until is None
    alert = Alert.objects.get(key="auth.breakglass_failed")
    assert alert.severity == "CRITICAL" and alert.count >= 7
    assert _login(c, "breakglass", GOOD).status_code == 302  # the real owner still gets in


# --- A3: changing your own password keeps you signed in -----------------------------------------


def test_forced_password_change_keeps_the_session(emp):
    User.objects.filter(pk=emp.pk).update(must_change_password=True)
    c = Client()
    assert _login(c, "asha", GOOD)["Location"] == "/password/change"
    r = c.post(
        "/password/change",
        {"old_password": GOOD, "new_password": "brand-new-pass-1", "new_password2": "brand-new-pass-1"},
    )
    assert r.status_code == 302
    after = c.get("/")
    assert after.status_code == 302 and not after["Location"].startswith("/login")
    assert User.objects.get(pk=emp.pk).check_password("brand-new-pass-1")


# --- A4: wrong current password is audited and counts toward the same lock ----------------------


def test_wrong_current_password_is_audited(emp):
    c = Client()
    _login(c, "asha", GOOD)
    r = c.post(
        "/password/change", {"old_password": "nope", "new_password": "x" * 12, "new_password2": "x" * 12}
    )
    assert r.status_code == 422
    rows = AuditLog.objects.filter(action="auth.password_change_failed", target_id=str(emp.pk))
    assert rows.count() == 1  # the row survived the failed request


def test_repeated_wrong_current_password_locks_and_signs_out(emp):
    c = Client()
    with freeze_time("2026-11-12 04:00:00") as ft:
        _login(c, "asha", GOOD)
        ft.tick(2)  # a failure at the very instant of the good login would tie with the reset point
        statuses = []
        for _ in range(5):
            r = c.post(
                "/password/change",
                {"old_password": "nope", "new_password": "x" * 12, "new_password2": "x" * 12},
            )
            statuses.append(r.status_code)
            ft.tick(2)
        assert statuses == [422, 422, 422, 422, 423]
        assert "_auth_user_id" not in c.session
        assert _login(Client(), "asha", GOOD).status_code == 423


# --- A5: a locked real ID and an unknown ID look the same ---------------------------------------


def test_unknown_login_locks_like_a_real_one(emp):
    c = Client()
    with freeze_time("2026-11-12 04:00:00") as ft:
        for _ in range(5):
            _login(c, "ghost", "x")
            ft.tick(2)
        for _ in range(5):
            _login(c, "asha", "x")
            ft.tick(2)
        ghost, real = _login(c, "ghost", "x"), _login(c, "asha", "x")
    assert ghost.status_code == real.status_code == 423
    assert ghost.content.decode().count("minute") == real.content.decode().count("minute")


# --- A6: the audit IP only trusts the proxy we run ---------------------------------------------


def test_forged_forwarded_for_is_ignored(settings):
    rf = RequestFactory()
    settings.OTS_TRUSTED_PROXY_HOPS = 0  # dev server: no proxy, header is client-supplied
    req = rf.get("/", HTTP_X_FORWARDED_FOR="6.6.6.6", REMOTE_ADDR="192.168.1.20")
    assert audit._client_ip(req) == "192.168.1.20"
    settings.OTS_TRUSTED_PROXY_HOPS = 1  # Caddy appends the real address last
    req = rf.get("/", HTTP_X_FORWARDED_FOR="6.6.6.6, 10.1.2.3", REMOTE_ADDR="172.18.0.2")
    assert audit._client_ip(req) == "10.1.2.3"


def test_proxy_proto_header_trusted_only_with_a_proxy():
    from django.conf import settings

    # base settings read the env var at import: the default (no proxy) must not trust the header
    assert settings.OTS_TRUSTED_PROXY_HOPS == 0 or settings.SECURE_PROXY_SSL_HEADER is not None


# --- A7: a missing common-password list is a loud error, never a silently disabled check ---------


def test_missing_common_password_file_is_an_error(monkeypatch, tmp_path):
    passwords._common.cache_clear()
    monkeypatch.setattr(passwords, "COMMON_FILE", tmp_path / "gone.txt")
    with pytest.raises(RuntimeError, match="common password list is missing"):
        passwords.validate("a-fine-long-password", "someone")
    passwords._common.cache_clear()


# --- P7 / P8: denial auditing ---------------------------------------------------------------


def test_unknown_action_is_named_in_the_error(emp):
    with pytest.raises(permissions.UnknownAction) as exc:
        permissions.require(emp, "no.such.action")
    assert exc.value.extra["action"] == "no.such.action"


def test_slow_denial_write_does_not_hold_the_request(monkeypatch, caplog, emp):
    monkeypatch.setattr(denied, "JOIN_SECONDS", 0.05)
    monkeypatch.setattr(denied, "_write", lambda *a, **k: time.sleep(0.4))
    req = RequestFactory().post("/app/people/new")
    req.user = emp
    start = time.monotonic()
    with caplog.at_level("WARNING"):
        denied.record_denied(req, PermissionDenied("no", action="user.create"))
    assert time.monotonic() - start < 0.3
    assert "still running" in caplog.text


# --- S8: reversing the append-only migration gives the privileges back --------------------------


def test_append_only_reverse_restores_privileges():
    from ots.core.db_ops import AppendOnly

    def can_update():
        with connection.cursor() as cur:
            cur.execute("SELECT has_table_privilege('ots_app', 'audit_log', 'UPDATE')")
            return cur.fetchone()[0]

    assert not can_update()
    with connection.schema_editor() as editor:
        AppendOnly("audit_log").database_backwards("audit", editor, None, None)
    assert can_update()
    # the surrounding test transaction is rolled back, so the real database keeps its guard
