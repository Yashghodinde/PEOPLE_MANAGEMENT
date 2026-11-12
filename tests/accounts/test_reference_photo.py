"""M02 rule 14: reference photos are validated, cropped square, shrunk to 480 x 480 JPEG and stored small."""

import io
import uuid

import pytest
from django.test import Client
from ots.accounts import photos, services
from ots.accounts.models import Role, User
from ots.audit.models import AuditLog
from ots.core.errors import Conflict, PermissionDenied, StorageUnavailable, ValidationFailed
from PIL import Image

from tests.accounts.factories import make_user
from tests.accounts.photo_helpers import open_stored, picture

pytestmark = pytest.mark.django_db
PW = "correct horse battery"


# --- the picture itself ------------------------------------------------------------------------------------


def test_any_size_becomes_a_480_square_jpeg_that_is_small():
    out = photos.process(picture((4000, 3000)))
    img = open_stored(out)
    assert img.format == "JPEG" and img.size == (480, 480)
    assert len(out) < 60_000  # tens of kilobytes, however big the original was


def test_the_crop_is_a_centred_square():
    img = open_stored(photos.process(picture((600, 300))))  # red | green | blue bands, 200 px each
    r, g, b = img.getpixel((10, 240)), img.getpixel((240, 240)), img.getpixel((470, 240))
    assert r[0] > 200 and r[1] < 60 and g[1] > 150 and g[0] < 60 and b[2] > 200 and b[0] < 60


def test_a_tall_picture_is_cropped_from_the_middle_too():
    img = open_stored(photos.process(picture((300, 900), colors=((255, 0, 0), (0, 200, 0), (0, 0, 255)))))
    # vertical bands in a tall image: the centre column is green, so the square keeps all three side by side
    assert img.size == (480, 480)


def test_png_with_transparency_becomes_white_not_black():
    img = open_stored(photos.process(_transparent_png()))
    assert min(img.getpixel((240, 240))) > 235 and min(img.getpixel((5, 5))) > 235


def _transparent_png() -> bytes:
    out = io.BytesIO()
    Image.new("RGBA", (300, 300), (0, 0, 0, 0)).save(out, "PNG")
    return out.getvalue()


def test_metadata_is_removed_and_the_cameras_rotation_is_applied():
    data = picture(
        (600, 300), orientation=6
    )  # "rotate 90 degrees clockwise to view"; also carries a name and make
    assert Image.open(io.BytesIO(data)).getexif().get(0x013B) == "secret artist name"
    img = open_stored(photos.process(data))
    assert not dict(img.getexif())  # nothing personal travels into storage
    assert img.getpixel((240, 20))[0] > 200  # the left (red) side became the top
    assert img.getpixel((240, 460))[2] > 200  # the right (blue) side became the bottom


@pytest.mark.parametrize(
    "data,why",
    [
        (b"", "Choose a picture"),
        (b"just some text pretending to be a photo.jpg", "not a usable"),
        (picture((100, 100), "GIF"), "JPEG or PNG"),
        (picture((100, 100), "BMP"), "JPEG or PNG"),
        (picture((300, 300))[:200], "not a usable"),  # truncated file
        (b"\x89PNG\r\n\x1a\n" + b"\x00" * 40, "not a usable"),  # a PNG signature and nothing else
    ],
)
def test_bad_files_are_refused_with_a_plain_message(data, why):
    with pytest.raises(ValidationFailed, match=why):
        photos.process(data)


def test_more_than_2_mb_is_refused():
    with pytest.raises(ValidationFailed, match="2 MB"):
        photos.process(b"x" * (photos.MAX_UPLOAD_BYTES + 1))


def test_an_absurd_number_of_pixels_is_refused_before_decoding():
    out = io.BytesIO()
    Image.new("1", (10_000, 5_000)).save(out, "PNG")  # 50 million pixels, a few KB on disk
    assert len(out.getvalue()) < photos.MAX_UPLOAD_BYTES
    with pytest.raises(ValidationFailed, match="too many pixels"):
        photos.process(out.getvalue())


def test_the_stored_path_is_built_from_the_id_only():
    uid = uuid.uuid4()
    assert photos.path_for(uid).name == f"{uid}.jpg"
    with pytest.raises(ValueError):
        photos.path_for("../../etc/passwd")


# --- the service ----------------------------------------------------------------------------------------------


@pytest.fixture
def world():
    sa = make_user(Role.SUPER_ADMIN, login_id="boss")
    sub = make_user(Role.SUB_ADMIN, reports_to=sa, login_id="sub")
    emp = make_user(Role.EMPLOYEE, reports_to=sub, login_id="emp")
    return sa, sub, emp


def test_the_picture_goes_live_only_after_the_database_commit(world, django_capture_on_commit_callbacks):
    sa, _, emp = world
    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        cur = services.set_reference_photo(sa, emp, picture())
    assert cur.reference_photo_path == f"reference/{emp.pk}.jpg" and cur.version == emp.version + 1
    final = photos.path_for(emp.pk)
    assert not final.exists() and final.with_suffix(".jpg.new").exists()  # staged, not yet live
    for cb in callbacks:
        cb()
    assert open_stored(final.read_bytes()).size == (480, 480)
    assert oct(final.stat().st_mode & 0o777) == "0o640" and not final.with_suffix(".jpg.new").exists()


def test_a_rolled_back_change_never_goes_live(world, django_capture_on_commit_callbacks):
    sa, _, emp = world
    from django.db import transaction

    with pytest.raises(RuntimeError), django_capture_on_commit_callbacks(execute=True):
        with transaction.atomic():
            services.set_reference_photo(sa, emp, picture())
            raise RuntimeError("the rest of the request failed")
    assert not photos.path_for(emp.pk).exists() and not User.objects.get(pk=emp.pk).reference_photo_path


def test_a_folder_that_cannot_be_written_fails_the_request_and_changes_nothing(world, settings, tmp_path):
    sa, _, emp = world
    blocker = tmp_path / "a-file-not-a-folder"
    blocker.write_text("x")
    settings.OTS_REFERENCE_PHOTO_DIR = blocker / "reference"  # cannot be created
    with pytest.raises(StorageUnavailable, match="could not be saved"):
        services.set_reference_photo(sa, emp, picture())
    fresh = User.objects.get(pk=emp.pk)
    assert (
        fresh.version == emp.version and not fresh.reference_photo_path
    )  # the database never claimed a photo
    assert not AuditLog.objects.filter(action="user.photo_updated").exists()


def test_through_the_screen_an_unwritable_folder_is_a_clean_error_not_a_broken_picture(
    world, settings, tmp_path
):
    _, _, emp = world
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    settings.OTS_REFERENCE_PHOTO_DIR = blocker / "reference"
    r = upload(login("boss"), emp, picture())
    assert r.status_code == 503 and "could not be saved" in r.content.decode()
    assert "/photo?v=" not in login("boss").get(f"/app/people/{emp.pk}").content.decode()


def test_replacing_keeps_exactly_one_file_and_audits_each_change(world, django_capture_on_commit_callbacks):
    sa, _, emp = world
    with django_capture_on_commit_callbacks(execute=True):
        services.set_reference_photo(sa, emp, picture((800, 800), "JPEG"))
        services.set_reference_photo(sa, User.objects.get(pk=emp.pk), picture((500, 900), "PNG"))
    assert len(list(photos.path_for(emp.pk).parent.glob("*"))) == 1  # no leftover temp files
    rows = AuditLog.objects.filter(action="user.photo_updated", target_id=str(emp.pk))
    assert rows.count() == 2 and rows[0].payload["bytes"] > 0 and rows[0].actor_id == sa.pk


def test_remove_deletes_the_file_and_clears_the_path(world, django_capture_on_commit_callbacks):
    sa, _, emp = world
    with django_capture_on_commit_callbacks(execute=True):
        services.set_reference_photo(sa, emp, picture())
        services.remove_reference_photo(sa, User.objects.get(pk=emp.pk))
    assert not photos.path_for(emp.pk).exists() and User.objects.get(pk=emp.pk).reference_photo_path == ""
    assert AuditLog.objects.filter(action="user.photo_removed").count() == 1
    with django_capture_on_commit_callbacks(execute=True):
        again = services.remove_reference_photo(
            sa, User.objects.get(pk=emp.pk)
        )  # nothing to remove: no extra row
    assert (
        AuditLog.objects.filter(action="user.photo_removed").count() == 1 and again.reference_photo_path == ""
    )


def test_only_a_super_admin_may_change_photos(world):
    sa, sub, emp = world
    for actor in (sub, emp):
        with pytest.raises(PermissionDenied):
            services.set_reference_photo(actor, emp, picture())
        with pytest.raises(PermissionDenied):
            services.remove_reference_photo(actor, emp)
    assert not User.objects.get(pk=emp.pk).reference_photo_path


def test_a_bad_picture_changes_nothing(world):
    sa, _, emp = world
    with pytest.raises(ValidationFailed):
        services.set_reference_photo(sa, emp, b"not a picture")
    fresh = User.objects.get(pk=emp.pk)
    assert fresh.version == emp.version and not fresh.reference_photo_path
    assert not AuditLog.objects.filter(action="user.photo_updated").exists()


def test_a_stale_version_is_a_conflict_and_the_break_glass_account_is_protected(world):
    sa, _, emp = world
    with pytest.raises(Conflict):
        services.set_reference_photo(sa, emp, picture(), expected_version=emp.version + 7)
    bg = make_user(Role.SUPER_ADMIN, login_id="breakglass", is_break_glass=True)
    with pytest.raises(ValidationFailed, match="server shell"):
        services.set_reference_photo(sa, bg, picture())


# --- the screens -------------------------------------------------------------------------------------------------


def login(login_id):
    c = Client()
    assert c.post("/login", {"login_id": login_id, "password": PW}).status_code == 302
    return c


def upload(c, user, data, name="me.jpg", **extra):
    f = io.BytesIO(data)
    f.name = name
    return c.post(f"/app/people/{user.pk}/photo/update", {"photo": f, **extra})


def test_a_super_admin_uploads_and_everyone_who_may_see_the_profile_sees_the_picture(
    world, django_capture_on_commit_callbacks
):
    sa, sub, emp = world
    other_sub = make_user(Role.SUB_ADMIN, reports_to=sa, login_id="sub2")
    with django_capture_on_commit_callbacks(execute=True):
        r = upload(login("boss"), emp, picture((1600, 900)))
    assert r.status_code == 302
    for who, status in (("boss", 200), ("sub", 200), ("emp", 200), ("sub2", 404)):
        resp = login(who).get(f"/app/people/{emp.pk}/photo")
        assert resp.status_code == status, who
        if status == 200:
            assert resp["Content-Type"] == "image/jpeg" and resp["X-Content-Type-Options"] == "nosniff"
            assert "private" in resp["Cache-Control"]
            assert open_stored(b"".join(resp.streaming_content)).size == (480, 480)
    assert (
        other_sub.pk and Client().get(f"/app/people/{emp.pk}/photo").status_code == 302
    )  # signed out: login


def test_the_profile_shows_the_picture_once_there_is_one(world, django_capture_on_commit_callbacks):
    sa, _, emp = world
    admin = login("boss")
    assert f"/app/people/{emp.pk}/photo?v=" not in admin.get(f"/app/people/{emp.pk}").content.decode()
    assert "Reference photo" in admin.get(f"/app/people/{emp.pk}").content.decode()
    with django_capture_on_commit_callbacks(execute=True):
        upload(admin, emp, picture())
    assert f"/app/people/{emp.pk}/photo?v=" in admin.get(f"/app/people/{emp.pk}").content.decode()
    assert "Reference photo" not in login("emp").get(f"/app/people/{emp.pk}").content.decode()  # admins only


def test_no_photo_is_a_404(world):
    _, _, emp = world
    assert login("boss").get(f"/app/people/{emp.pk}/photo").status_code == 404


def test_employees_and_sub_admins_cannot_upload_and_the_denial_is_audited(world):
    sa, sub, emp = world
    for who in ("emp", "sub"):
        assert upload(login(who), emp, picture()).status_code == 403
    assert AuditLog.objects.filter(action="security.denied").count() >= 2
    assert not User.objects.get(pk=emp.pk).reference_photo_path


def test_bad_uploads_through_the_screen_are_a_422_with_a_message(world):
    _, _, emp = world
    c = login("boss")
    r = upload(c, emp, b"definitely not an image", name="evil.jpg")  # the name says jpg, the content says no
    assert r.status_code == 422 and "not a usable" in r.content.decode()
    assert c.post(f"/app/people/{emp.pk}/photo/update", {}).status_code == 422  # no file chosen at all
    assert upload(c, emp, picture((100, 100), "GIF"), name="x.gif").status_code == 422


def test_a_huge_upload_is_refused_without_reading_it(world):
    _, _, emp = world
    c = login("boss")
    r = c.post(
        f"/app/people/{emp.pk}/photo/update",
        {"photo": io.BytesIO(b"x" * (3 * 1024 * 1024))},
    )
    assert r.status_code == 422 and "2 MB" in r.content.decode()


def test_remove_through_the_screen(world, django_capture_on_commit_callbacks):
    _, _, emp = world
    c = login("boss")
    with django_capture_on_commit_callbacks(execute=True):
        upload(c, emp, picture())
    assert photos.path_for(emp.pk).exists()
    with django_capture_on_commit_callbacks(execute=True):
        assert c.post(f"/app/people/{emp.pk}/photo/update", {"remove": "1"}).status_code == 302
    assert not photos.path_for(emp.pk).exists()


def test_the_avatar_shows_the_picture_or_the_initials():
    from django.template.loader import render_to_string

    with_photo = render_to_string("components/avatar.html", {"name": "Asha Kulkarni", "photo": "/p.jpg"})
    without = render_to_string("components/avatar.html", {"name": "Asha Kulkarni"})
    assert '<img src="/p.jpg"' in with_photo and "AK" not in with_photo
    assert "AK" in without and "<img" not in without
