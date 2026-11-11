"""F-ID-4: 5 wrong passwords in 15 minutes lock the account for 15 minutes; it unlocks by itself."""

import pytest
from django.test import Client
from freezegun import freeze_time
from ots.accounts.models import Role, User
from ots.audit.models import AuditLog

from tests.accounts.factories import make_user

pytestmark = pytest.mark.django_db
GOOD = "correct horse battery"


def _post(c, login, pw):
    return c.post("/login", {"login_id": login, "password": pw})


@pytest.fixture
def emp():
    sa = make_user(Role.SUPER_ADMIN)
    return make_user(Role.EMPLOYEE, reports_to=sa, login_id="asha")


def test_lockout_and_auto_unlock(emp):
    c = Client()
    with freeze_time("2026-11-12 04:00:00") as ft:
        for _ in range(4):
            assert _post(c, "asha", "wrong").status_code == 400
            ft.tick(60)
        r = _post(c, "asha", "wrong")  # 5th failure within 15 min
        assert r.status_code == 423
        assert "15 minute" in r.content.decode()
        assert User.objects.get(pk=emp.pk).locked_until is not None
        # even the right password is refused while locked
        assert _post(c, "asha", GOOD).status_code == 423
        assert AuditLog.objects.filter(action="auth.locked", target_id=str(emp.pk)).count() == 1
        ft.tick(14 * 60)
        assert _post(c, "asha", GOOD).status_code == 423
        ft.tick(2 * 60)  # lock (15 min from the 5th failure) has expired
        r = _post(c, "asha", GOOD)
        assert r.status_code == 302
    u = User.objects.get(pk=emp.pk)
    assert u.failed_logins == 0 and u.locked_until is None


def test_failures_outside_window_do_not_lock(emp):
    c = Client()
    with freeze_time("2026-11-12 04:00:00") as ft:
        for _ in range(4):
            _post(c, "asha", "wrong")
            ft.tick(5 * 60)  # 20 minutes in total: never 5 inside 15 minutes
        assert _post(c, "asha", "wrong").status_code == 400
        assert User.objects.get(pk=emp.pk).locked_until is None


def test_every_attempt_is_audited_with_ip(emp, settings):
    settings.OTS_TRUSTED_PROXY_HOPS = (
        1  # one proxy (Caddy) in front: its entry is the last in X-Forwarded-For
    )
    c = Client(HTTP_X_FORWARDED_FOR="10.1.2.3")
    _post(c, "asha", "wrong")
    _post(c, "nobody", "wrong")
    _post(c, "asha", GOOD)
    rows = list(AuditLog.objects.filter(action__startswith="auth.").order_by("id"))
    assert [r.action for r in rows] == ["auth.login_failed", "auth.login_failed", "auth.login_ok"]
    assert all(r.client_ip == "10.1.2.3" for r in rows)
    assert rows[1].payload["login_id"] == "nobody"
