"""Fixtures shared by the file tests and the F-FL failure tests (registered in tests/conftest.py)."""

import pytest
from ots.accounts.models import Role

from tests.accounts.factories import make_user
from tests.tasks.factories import make_task


@pytest.fixture
def dirs(settings, tmp_path):
    settings.OTS_STAGING_DIR = tmp_path / "staging"
    settings.OTS_BLOB_DIR = tmp_path / "blobs"
    settings.OTS_DISK_FREE_MIN_PERCENT = 0  # the real disk is not what these tests are about
    settings.OTS_UPLOAD_PIECE_BYTES = 1024  # small pieces, so a few KB exercises the protocol
    settings.OTS_MANIFEST_DIR = tmp_path / "manifests"  # never the real /srv/data
    return tmp_path


@pytest.fixture
def office():
    sa = make_user(Role.SUPER_ADMIN, login_id="boss", full_name="Boss Person")
    sub = make_user(Role.SUB_ADMIN, reports_to=sa, login_id="sub", full_name="Sub Admin")
    emp = make_user(Role.EMPLOYEE, reports_to=sub, login_id="emp", full_name="Rahul Sharma")
    other = make_user(Role.EMPLOYEE, reports_to=sa, login_id="other", full_name="Other Person")
    task = make_task(sub, emp, title="Monthly Sales Report")
    return {"sa": sa, "sub": sub, "emp": emp, "other": other, "task": task}


@pytest.fixture
def remote(settings, tmp_path):
    """A fake SharePoint behind the real storage factory, so the real queue and the real file handlers run."""
    from ots.storage.factory import get_provider, reset_provider

    settings.OTS_STORAGE_BACKEND = "fake"
    settings.OTS_STORAGE_FAKE_DIR = tmp_path / "remote"
    reset_provider()
    yield get_provider()
    reset_provider()


@pytest.fixture
def file_handlers():
    """Other tests clear the storage handler registry; the file tests need the real ones in place."""
    from ots.files import handlers
    from ots.storage.handlers import clear_handlers

    clear_handlers()
    handlers.register()
    yield
    clear_handlers()
    handlers.register()
