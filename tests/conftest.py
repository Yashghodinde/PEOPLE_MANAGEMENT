import pytest


# ---- storage fixtures shared by tests/storage and tests/failure ---------------------------------------------
@pytest.fixture
def fake(tmp_path):
    from ots.storage.fake import LocalDirProvider

    return LocalDirProvider(tmp_path / "remote")


@pytest.fixture(autouse=True)
def _field_key(settings):
    """uploadUrl is stored encrypted; give every test a valid key."""
    from cryptography.fernet import Fernet

    settings.OTS_FIELD_ENCRYPTION_KEY = Fernet.generate_key().decode()


@pytest.fixture(autouse=True)
def _photo_dir(settings, tmp_path):
    """Reference photos go to a temporary folder in tests, never to /srv/data."""
    settings.OTS_REFERENCE_PHOTO_DIR = tmp_path / "reference-photos"


# ---- the small company used by the task tests (tests/tasks and tests/failure) ----------------------------
@pytest.fixture
def org():
    """sa (Super Admin) > mgr, other (Sub-Admins) > emp, peer (mgr's team), outsider (other's team)."""
    from ots.accounts.models import Role

    from tests.accounts.factories import make_user

    sa = make_user(Role.SUPER_ADMIN, login_id="boss", full_name="Boss Person")
    mgr = make_user(Role.SUB_ADMIN, reports_to=sa, login_id="mgr", full_name="Arjun Rao")
    other = make_user(Role.SUB_ADMIN, reports_to=sa, login_id="other", full_name="Neha Shah")
    emp = make_user(Role.EMPLOYEE, reports_to=mgr, login_id="emp", full_name="Asha Kulkarni")
    peer = make_user(Role.EMPLOYEE, reports_to=mgr, login_id="peer", full_name="Karan Malhotra")
    outsider = make_user(Role.EMPLOYEE, reports_to=other, login_id="outsider", full_name="Ravi Patel")
    return {"sa": sa, "mgr": mgr, "other": other, "emp": emp, "peer": peer, "outsider": outsider}


@pytest.fixture
def task(org):
    """A task created by the manager and assigned to emp."""
    from tests.tasks.factories import make_task

    return make_task(org["mgr"], org["emp"])


@pytest.fixture
def assignment(task, org):
    return task.assignments.get(user=org["emp"])
