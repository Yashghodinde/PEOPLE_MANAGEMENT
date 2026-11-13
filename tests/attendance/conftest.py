import io

import pytest
from PIL import Image


@pytest.fixture(autouse=True)
def selfie_stage_on(settings):
    settings.OTS_SELFIE_STAGE_ENABLED = True


def jpeg(w=480, h=360, fmt="JPEG"):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (120, 90, 60)).save(buf, fmt)
    return buf.getvalue()


@pytest.fixture
def make_ws():
    """An open work session with a real Django session behind it."""
    from django.contrib.sessions.backends.db import SessionStore
    from ots.attendance.models import WorkSession
    from ots.core.clock import now, today_local

    def make(user, **kw):
        store = SessionStore()
        store["_auth_user_id"] = str(user.pk)
        store.create()
        defaults = {"user": user, "work_date": today_local(), "login_at": now(), "selfie_path": "x.jpg",
                    "session_key": store.session_key, "live_connected": True}  # fmt: skip
        defaults.update(kw)
        return WorkSession.objects.create(**defaults)

    return make


@pytest.fixture
def worker(db):
    from ots.accounts.models import Role

    from tests.accounts.factories import make_user

    boss = make_user(Role.SUPER_ADMIN, login_id="boss")
    return make_user(Role.EMPLOYEE, reports_to=boss, login_id="asha")
