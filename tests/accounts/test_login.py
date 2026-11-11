import io

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client
from ots.accounts import passwords
from ots.accounts.models import Role, User
from ots.audit.models import AuditLog
from ots.core.errors import ValidationFailed
from ots.core.models import Alert

from tests.accounts.factories import make_user

pytestmark = pytest.mark.django_db
GOOD = "correct horse battery"


@pytest.fixture
def sa():
    return make_user(Role.SUPER_ADMIN, login_id="boss")


def test_login_logout_and_case_insensitive_login(sa):
    c = Client()
    assert c.get("/login").status_code == 200
    r = c.post("/login", {"login_id": "BOSS", "password": GOOD})
    assert r.status_code == 302 and r["Location"] == "/"
    assert "_auth_user_id" in c.session
    assert User.objects.get(pk=sa.pk).last_login_at is not None
    assert c.post("/logout").status_code == 302
    assert "_auth_user_id" not in c.session


def test_wrong_password_and_unknown_user_look_identical(sa):
    c = Client()
    a = c.post("/login", {"login_id": "boss", "password": "nope"})
    b = c.post("/login", {"login_id": "ghost", "password": "nope"})
    assert a.status_code == b.status_code == 400
    assert "Wrong login ID or password." in a.content.decode()
    assert "Wrong login ID or password." in b.content.decode()


def test_inactive_user_cannot_log_in(sa):
    emp = make_user(Role.EMPLOYEE, reports_to=sa, login_id="gone", is_active=False)
    assert emp.pk
    assert Client().post("/login", {"login_id": "gone", "password": GOOD}).status_code == 400


def test_open_redirect_is_refused(sa):
    r = Client().post("/login", {"login_id": "boss", "password": GOOD, "next": "https://evil.example/x"})
    assert r["Location"] == "/"


def test_must_change_password_flow(sa):
    emp = make_user(Role.EMPLOYEE, reports_to=sa, login_id="newbie", must_change_password=True)
    c = Client()
    r = c.post("/login", {"login_id": "newbie", "password": GOOD})
    assert r["Location"] == "/password/change"
    assert c.get("/some/page").status_code == 302  # forced back
    assert c.get("/password/change").status_code == 200
    r = c.post("/password/change", {"old_password": GOOD, "new_password": "x", "new_password2": "x"})
    assert r.status_code == 422
    r = c.post(
        "/password/change",
        {"old_password": GOOD, "new_password": "a-brand-new-pass", "new_password2": "a-brand-new-pass"},
    )
    assert r.status_code == 302
    assert not User.objects.get(pk=emp.pk).must_change_password
    assert c.get("/some/page").status_code == 404  # no longer redirected


def test_selfie_stage_flag_defers_the_session(sa, settings):
    settings.OTS_SELFIE_STAGE_ENABLED = True
    c = Client()
    r = c.post("/login", {"login_id": "boss", "password": GOOD})
    assert r["Location"] == "/login/selfie"
    assert "_auth_user_id" not in c.session and c.session["pending_user_id"] == str(sa.pk)


def test_break_glass_login_raises_critical_alert(sa):
    make_user(Role.SUPER_ADMIN, login_id="breakglass", is_break_glass=True)
    Client().post("/login", {"login_id": "breakglass", "password": GOOD})
    a = Alert.objects.get(key="auth.breakglass_used")
    assert a.severity == "CRITICAL"
    assert AuditLog.objects.filter(action="auth.login_ok", payload__break_glass=True).exists()


def test_password_policy(tmp_path, monkeypatch):
    f = tmp_path / "common.txt"
    f.write_text("password123\n")
    monkeypatch.setattr(passwords, "COMMON_FILE", f)
    passwords._common.cache_clear()
    try:
        passwords.validate("a-fine-password", "bob")
        for bad, login in (("short", "x"), ("longenoughlogin", "longenoughlogin"), ("Password123", "x")):
            with pytest.raises(ValidationFailed):
                passwords.validate(bad, login)
    finally:
        passwords._common.cache_clear()


def test_bundled_common_list_has_10k_entries():
    passwords._common.cache_clear()
    assert len(passwords._common()) >= 9000
    with pytest.raises(ValidationFailed):
        passwords.validate("1234567890")


def test_bootstrap_admins_creates_both_and_refuses_twice():
    out = io.StringIO()
    call_command("bootstrap_admins", "--login", "owner", "--name", "The Owner", stdout=out)
    main, glass = User.objects.get(login_id="owner"), User.objects.get(login_id="breakglass")
    assert main.role == glass.role == Role.SUPER_ADMIN and glass.is_break_glass and not main.is_break_glass
    text = out.getvalue()
    pw_glass = text.split("password: ")[2].split()[0]
    assert len(pw_glass) == 24 and glass.check_password(pw_glass)
    with pytest.raises(CommandError):
        call_command("bootstrap_admins", "--login", "again", "--name", "X", stdout=io.StringIO())


def test_reset_master_writes_audit_and_alert(sa):
    out = io.StringIO()
    call_command("reset_master", "--login", "boss", stdout=out)
    temp = out.getvalue().split("boss: ")[1].split()[0]
    u = User.objects.get(pk=sa.pk)
    assert u.check_password(temp) and u.must_change_password
    assert AuditLog.objects.filter(action="breakglass.reset_master", target_id=str(sa.pk)).count() == 1
    assert Alert.objects.get(key="auth.reset_master").severity == "CRITICAL"
    with pytest.raises(CommandError):
        call_command("reset_master", "--login", "nobody", stdout=io.StringIO())
