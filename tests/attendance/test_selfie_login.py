"""M06.1: password first, then the selfie; the session exists only after the picture is saved."""

import io
import uuid

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from ots.attendance import selfies
from ots.attendance.models import WorkSession
from ots.audit.models import AuditLog
from ots.core.errors import ValidationFailed
from PIL import Image

from tests.accounts.factories import make_user
from tests.attendance.conftest import jpeg

pytestmark = pytest.mark.django_db
PW = "correct horse battery"


@pytest.fixture
def emp():
    from ots.accounts.models import Role

    boss = make_user(Role.SUPER_ADMIN, login_id="boss")
    return make_user(Role.EMPLOYEE, reports_to=boss, login_id="asha")


def stage_one(c, login_id="asha"):
    r = c.post("/login", {"login_id": login_id, "password": PW})
    assert r.status_code == 302 and r["Location"] == "/login/selfie"


def send(c, data=None, key=None, **extra):
    data = {"selfie": SimpleUploadedFile("s.jpg", jpeg(), "image/jpeg"), "device_id": "dev-1", **(data or {})}
    return c.post("/login/selfie", data, HTTP_IDEMPOTENCY_KEY=key or str(uuid.uuid4()), **extra)


def test_password_alone_creates_no_session(emp):
    c = Client()
    stage_one(c)
    assert c.get("/app/dashboard").status_code == 302  # still logged out
    assert WorkSession.objects.count() == 0


def test_selfie_completes_the_login_in_one_step(emp, settings, django_capture_on_commit_callbacks):
    c = Client()
    stage_one(c)
    with django_capture_on_commit_callbacks(execute=True):
        r = send(c)
    assert r.status_code == 200 and r.json()["ok"]
    ws = WorkSession.objects.get()
    assert ws.user_id == emp.pk and ws.device_id == "dev-1" and ws.session_key
    assert (settings.OTS_SELFIE_DIR / ws.selfie_path).exists()
    assert oct((settings.OTS_SELFIE_DIR / ws.selfie_path).stat().st_mode)[-3:] == "640"
    assert AuditLog.objects.filter(action="auth.session_started").count() == 1
    assert c.get("/app/dashboard").status_code == 200


def test_the_stored_picture_has_no_exif_and_is_small():
    src = io.BytesIO()
    img = Image.new("RGB", (480, 360), (10, 20, 30))
    exif = Image.Exif()
    exif[0x010F] = "SecretCameraMaker"
    img.save(src, "JPEG", exif=exif.tobytes())
    out = selfies.process(src.getvalue())
    assert b"SecretCameraMaker" not in out and len(out) < 200 * 1024


@pytest.mark.parametrize(
    "data",
    [b"", b"not an image", jpeg(100, 100), jpeg(2000, 1500), jpeg(fmt="PNG"), b"\xff\xd8\xff" + b"0" * 500],
)
def test_bad_pictures_are_refused(data):
    with pytest.raises(ValidationFailed):
        selfies.process(data)


def test_a_picture_over_200kb_is_refused():
    with pytest.raises(ValidationFailed):
        selfies.process(jpeg() + b"0" * (200 * 1024))


def test_expired_pending_login_is_refused(emp, freezer=None):
    from freezegun import freeze_time

    c = Client()
    with freeze_time("2026-11-12 10:00:00"):
        stage_one(c)
    with freeze_time("2026-11-12 10:04:00"):  # 4 minutes later: past the 3 minute limit
        r = send(c)
        assert r.status_code == 401
    assert WorkSession.objects.count() == 0


def test_posting_a_selfie_without_a_password_step_is_refused(emp):
    assert send(Client()).status_code == 401


def test_camera_unavailable_is_audited_and_no_session_is_made(emp):  # F-AT-3
    c = Client()
    stage_one(c)
    r = c.post("/login/selfie", {"camera": "unavailable"}, HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()))
    assert r.status_code == 403 and "Camera required" in r.json()["message"]
    assert AuditLog.objects.filter(action="auth.selfie_unavailable").count() == 1
    assert WorkSession.objects.count() == 0 and c.get("/app/dashboard").status_code == 302


def test_the_same_key_twice_makes_one_session(emp):  # F-AT-6
    c = Client()
    stage_one(c)
    key = str(uuid.uuid4())
    assert send(c, key=key).status_code == 200
    assert send(c, key=key).status_code == 200
    assert WorkSession.objects.count() == 1


def test_a_failed_save_leaves_nothing_behind(emp, settings, tmp_path):  # F-AT-7
    blocker = tmp_path / "blocked"
    blocker.write_text("a file where a folder should be")
    settings.OTS_SELFIE_DIR = blocker
    c = Client()
    stage_one(c)
    r = send(c)
    assert r.status_code >= 500 or r.status_code == 503
    assert WorkSession.objects.count() == 0
    assert AuditLog.objects.filter(action="auth.session_started").count() == 0
    assert c.get("/app/dashboard").status_code == 302  # no session was created
    settings.OTS_SELFIE_DIR = tmp_path / "ok"
    assert send(c).status_code == 200  # the person can simply try again


def test_must_change_password_goes_there_after_the_selfie(emp):
    emp.must_change_password = True
    emp.save()
    c = Client()
    stage_one(c)
    assert send(c).json()["next"] == "/password/change"


def test_the_selfie_page_actually_shows_the_camera_and_button(emp):
    c = Client()
    stage_one(c)
    body = c.get("/login/selfie").content.decode()
    assert 'id="selfie-video"' in body and 'id="selfie-capture"' in body and "js/selfie.js" in body
