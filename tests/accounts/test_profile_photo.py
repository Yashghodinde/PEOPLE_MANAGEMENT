"""IMP-16.6 (PHOTO-01..10): a person's own profile photo; the reference photo is separate attendance evidence; a
picture that cannot load shows initials."""

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.template import Context, Template
from ots.accounts import photos, services
from ots.accounts.models import Role
from ots.audit.models import AuditLog
from ots.core.errors import PermissionDenied, ValidationFailed

from tests.accounts.factories import make_user
from tests.accounts.photo_helpers import picture
from tests.files.conftest import login

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.usefixtures("dirs")]


@pytest.fixture
def world(office):
    hr = make_user(Role.HR, reports_to=office["sa"], login_id="hrp")
    return {**office, "hr": hr}


def upload(c, who, data=None, name="me.jpg"):
    return c.post(
        f"/app/people/{who.pk}/profile-photo",
        {"photo": SimpleUploadedFile(name, data or picture((900, 600)), content_type="image/jpeg")},
    )


def test_a_person_uploads_their_own_photo_and_it_is_stored_small_and_square(world):
    emp = world["emp"]
    r = upload(login("emp"), emp)
    assert r.status_code in (200, 302)
    emp.refresh_from_db()
    assert emp.profile_photo_path == f"profile/{emp.pk}.jpg"
    stored = photos.path_for(emp.pk, "profile")
    assert stored.is_file() and stored.stat().st_size < 100_000
    from PIL import Image

    assert Image.open(stored).size == (480, 480)
    assert AuditLog.objects.filter(action="user.profile_photo_updated").count() == 1


def test_the_photo_is_served_to_people_who_may_see_the_profile_and_is_private_to_cookies(world):
    upload(login("emp"), world["emp"])
    r = login("sub").get(f"/app/people/{world['emp'].pk}/photo")
    assert r.status_code == 200 and r["Content-Type"] == "image/jpeg"
    assert "Cookie" in r["Vary"] and "private" in r["Cache-Control"]


def test_no_photo_is_a_404_so_the_page_shows_initials(world):
    assert login("sub").get(f"/app/people/{world['emp'].pk}/photo").status_code == 404


def test_the_reference_photo_is_the_fallback_and_stays_separate(world):
    services.set_reference_photo(world["sa"], world["emp"], picture((500, 500)))
    world["emp"].refresh_from_db()
    assert world["emp"].reference_photo_path and not world["emp"].profile_photo_path
    assert (
        login("sub").get(f"/app/people/{world['emp'].pk}/photo").status_code == 200
    )  # nothing that showed disappears
    upload(login("emp"), world["emp"], picture((500, 500), colors=((0, 0, 255),) * 3))
    assert (
        photos.path_for(world["emp"].pk).is_file() and photos.path_for(world["emp"].pk, "profile").is_file()
    )
    assert (
        photos.path_for(world["emp"].pk).read_bytes()
        != photos.path_for(world["emp"].pk, "profile").read_bytes()
    )
    world["emp"].refresh_from_db()
    assert world["emp"].reference_photo_path  # the attendance picture is untouched


def test_hr_and_managers_and_colleagues_cannot_change_someone_elses_photo(world):
    for who in ("hrp", "sub", "other"):
        r = upload(login(who), world["emp"])
        assert r.status_code in (403, 404), who
    world["emp"].refresh_from_db()
    assert not world["emp"].profile_photo_path
    with pytest.raises(PermissionDenied):
        services.set_profile_photo(world["hr"], world["emp"], picture())


def test_the_super_admin_may_recover_a_photo_for_someone(world):
    services.set_profile_photo(world["sa"], world["emp"], picture())
    world["emp"].refresh_from_db()
    assert world["emp"].profile_photo_path


def test_a_bad_file_changes_nothing(world):
    emp = world["emp"]
    for bad in (b"", b"not an image", picture((100, 100), fmt="GIF") if False else b"GIF89a....."):
        with pytest.raises(ValidationFailed):
            services.set_profile_photo(emp, emp, bad)
    emp.refresh_from_db()
    assert not emp.profile_photo_path and not photos.path_for(emp.pk, "profile").exists()


def test_replacing_changes_the_cache_key_so_the_new_picture_shows_at_once(world):
    emp = world["emp"]
    upload(login("emp"), emp)
    emp.refresh_from_db()
    tpl = Template("{% load ots_ui %}{{ u|photo_url }}")
    first = tpl.render(Context({"u": emp}))
    upload(login("emp"), emp, picture((700, 700), colors=((255, 0, 0),) * 3))
    emp.refresh_from_db()
    second = tpl.render(Context({"u": emp}))
    assert first != second and second.startswith(f"/app/people/{emp.pk}/photo?v=")


def test_two_people_never_share_a_picture_or_a_url(world):
    upload(login("emp"), world["emp"])
    world["emp"].refresh_from_db()
    other = make_user(Role.EMPLOYEE, reports_to=world["sub"], login_id="e9", full_name="Esha Nine")
    tpl = Template("{% load ots_ui %}{{ u|photo_url }}")
    assert tpl.render(Context({"u": other})) == ""  # no photo: initials
    assert tpl.render(Context({"u": world["emp"]})) != tpl.render(Context({"u": other}))
    assert login("sub").get(f"/app/people/{other.pk}/photo").status_code == 404


def test_remove_returns_to_the_fallback_and_deletes_the_file(world):
    emp = world["emp"]
    upload(login("emp"), emp)
    r = login("emp").post(f"/app/people/{emp.pk}/profile-photo", {"remove": "1"})
    assert r.status_code in (200, 302)
    emp.refresh_from_db()
    assert not emp.profile_photo_path and not photos.path_for(emp.pk, "profile").exists()
    assert AuditLog.objects.filter(action="user.profile_photo_removed").count() == 1


def test_the_profile_page_offers_a_profile_photo_card_only_to_its_owner_and_the_super_admin(world):
    mine = login("emp").get(f"/app/people/{world['emp'].pk}").content.decode()
    assert "Profile photo" in mine and "Save photo" in mine and "data-preview" in mine
    assert "Attendance reference photo" not in mine  # that one is the Super Admin's
    theirs = login("sub").get(f"/app/people/{world['emp'].pk}").content.decode()
    assert "Save photo" not in theirs
    boss = login("boss").get(f"/app/people/{world['emp'].pk}").content.decode()
    assert "Save photo" in boss and "Attendance reference photo" in boss


def test_avatars_use_the_photo_url_and_fall_back_to_initials_when_it_cannot_load(world):
    from django.template.loader import render_to_string

    with_photo = render_to_string(
        "components/avatar.html", {"name": "Asha Kulkarni", "photo": "/app/people/x/photo?v=2"}
    )
    assert "data-avatar" in with_photo and 'data-name="Asha Kulkarni"' in with_photo
    assert "AK" in render_to_string("components/avatar.html", {"name": "Asha Kulkarni", "photo": ""})
    page = login("emp").get("/app/dashboard").content.decode()
    initials = "".join(part[0] for part in world["emp"].full_name.split()[:2]).upper()
    assert initials in page  # the top-bar avatar: initials until a photo is uploaded
    upload(login("emp"), world["emp"])
    assert f"/app/people/{world['emp'].pk}/photo?v=" in login("emp").get("/app/dashboard").content.decode()
