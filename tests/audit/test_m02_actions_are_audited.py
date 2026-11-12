"""M03 done-gate: every M02 action leaves an audit row. Loops the M02 services, login paths and commands.

If a new write service is added to ots.accounts.services without a case here, the last test fails, so the
audit guarantee cannot quietly lapse.
"""

import inspect
import io

import pytest
from django.core.management import call_command
from django.test import Client
from ots.accounts import services
from ots.accounts.models import Role, User
from ots.audit.models import AuditLog

from tests.accounts.factories import make_user

pytestmark = pytest.mark.django_db
PW = "a-long-enough-password"


@pytest.fixture
def world():
    sa = make_user(Role.SUPER_ADMIN, login_id="boss")
    sa2 = make_user(Role.SUPER_ADMIN)
    sub = make_user(Role.SUB_ADMIN, reports_to=sa)
    sub2 = make_user(Role.SUB_ADMIN, reports_to=sa)
    emp = make_user(Role.EMPLOYEE, reports_to=sub, login_id="asha")
    return {"sa": sa, "sa2": sa2, "sub": sub, "sub2": sub2, "emp": emp}


def _create(w):
    return services.create_user(
        w["sa"], login_id="newbie", employee_id="E-new", full_name="New Person", role=Role.EMPLOYEE,
        reports_to=w["sub"], password=PW,
    )  # fmt: skip


def _photo():
    from tests.accounts.photo_helpers import picture

    return picture((300, 300))


SERVICE_CASES = {
    "create_user": ("user.created", _create),
    "update_profile": (
        "user.updated",
        lambda w: services.update_profile(w["sa"], w["emp"], designation="Clerk"),
    ),
    "set_manager": ("user.manager_changed", lambda w: services.set_manager(w["sa"], w["emp"], w["sub2"])),
    "deactivate_user": ("user.deactivated", lambda w: services.deactivate_user(w["sa"], w["emp"])),
    "reactivate_user": (
        "user.reactivated",
        lambda w: (services.deactivate_user(w["sa"], w["emp"]), services.reactivate_user(w["sa"], w["emp"])),
    ),
    "reset_password": (
        "user.password_reset",
        lambda w: services.reset_password(w["sa"], w["emp"], "temporary-pass-9"),
    ),
    "set_reference_photo": (
        "user.photo_updated",
        lambda w: services.set_reference_photo(w["sa"], w["emp"], _photo()),
    ),
    "remove_reference_photo": (
        "user.photo_removed",
        lambda w: (
            services.set_reference_photo(w["sa"], w["emp"], _photo()),
            services.remove_reference_photo(w["sa"], User.objects.get(pk=w["emp"].pk)),
        ),
    ),
    "change_password": (
        "user.password_changed",
        lambda w: services.change_password(w["emp"], "correct horse battery", "brand-new-pass-1"),
    ),
}


@pytest.mark.parametrize("name", sorted(SERVICE_CASES))
def test_every_service_writes_its_audit_row(world, name):
    action, run = SERVICE_CASES[name]
    before = AuditLog.objects.filter(action=action).count()
    run(world)
    assert AuditLog.objects.filter(action=action).count() == before + 1


def test_every_write_service_has_a_case_here():
    public = {
        n
        for n, fn in inspect.getmembers(services, inspect.isfunction)
        if fn.__module__ == services.__name__ and not n.startswith("_")
    }
    assert public == set(SERVICE_CASES), (
        "a public function in ots.accounts.services has no audit test (add it to SERVICE_CASES): "
        f"{sorted(public ^ set(SERVICE_CASES))}"
    )


def test_login_paths_are_audited(world):
    c = Client()
    c.post("/login", {"login_id": "asha", "password": "wrong"})
    c.post("/login", {"login_id": "ghost", "password": "wrong"})
    c.post("/login", {"login_id": "asha", "password": "correct horse battery"})
    for _ in range(5):
        Client().post("/login", {"login_id": "boss", "password": "wrong"})
    Client().post("/login", {"login_id": "boss", "password": "correct horse battery"})  # locked: refused
    seen = set(AuditLog.objects.filter(action__startswith="auth.").values_list("action", flat=True))
    assert {"auth.login_ok", "auth.login_failed", "auth.locked", "auth.login_blocked"} <= seen


def test_wrong_password_change_is_audited(world):
    with pytest.raises(Exception):  # noqa: B017, PT011  (ValidationFailed)
        services.change_password(world["emp"], "wrong", "brand-new-pass-1")
    assert AuditLog.objects.filter(action="auth.password_change_failed").count() == 1


def test_denied_request_is_audited(world):
    c = Client()
    c.post("/login", {"login_id": "asha", "password": "correct horse battery"})
    r = c.get("/app/people/new")
    assert r.status_code == 403
    assert AuditLog.objects.filter(action="security.denied", actor_id=world["emp"].pk).count() == 1


def test_admin_commands_are_audited(world):
    out = io.StringIO()
    call_command("reset_master", login=world["sa"].login_id, stdout=out)
    assert AuditLog.objects.filter(action="breakglass.reset_master").count() == 1


def test_bootstrap_admins_audits_both_accounts():
    call_command("bootstrap_admins", login="owner", name="Owner Person", stdout=io.StringIO())
    assert AuditLog.objects.filter(action="user.created").count() == 2
    assert AuditLog.objects.filter(action="breakglass.created").count() == 1
