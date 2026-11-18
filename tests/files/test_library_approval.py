"""IMP-15.5: staff upload straight into a Library folder; the file waits, pale, for the manager above them
(LIB-01..12). Rejected means gone from view, not erased."""

import uuid

import pytest
from ots.audit.models import AuditLog
from ots.core.errors import NotFound, PermissionDenied, ValidationFailed
from ots.files import library
from ots.files.models import File, LibraryReview, LibraryState
from ots.notifications.models import Kind, Notification

from tests.files.conftest import login, start, upload_all

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.usefixtures("dirs")]
DATA = bytes((i * 17) % 256 for i in range(700))


def send(who, folder, name="2026-11-17_Policy-draft.txt", data=DATA, **extra):
    upload_all(
        login(who), data, uuid.uuid4(), None, name=name, scope="library", folder_id=str(folder.pk), **extra
    )
    return File.objects.order_by("-created_at", "-pk").first()


@pytest.fixture
def lib(office):
    return library.create_folder(office["sa"], "Finance")


def test_an_employee_upload_is_pending_and_the_super_admin_upload_is_not(office, lib):
    f = send("emp", lib)
    assert (
        f.library_state == LibraryState.PENDING
        and f.library_folder_id == lib.pk
        and f.owner_id == office["emp"].pk
    )
    g = send("boss", lib, name="2026-11-17_Official.txt", data=DATA + b"1")
    assert g.library_state == LibraryState.APPROVED


def test_nobody_else_can_see_a_pending_file_by_any_route(office, lib):
    f = send("emp", lib)
    v = f.current_version
    page = lambda who: login(who).get(f"/files/library?folder={lib.pk}").content.decode()  # noqa: E731
    assert f.display_name in page("emp")  # the sender
    assert f.display_name in page("sub")  # their manager
    assert f.display_name in page("boss")  # the Super Admin
    for stranger in ("other",):
        assert f.display_name not in page(stranger)
        assert login(stranger).get(f"/files/v/{v.pk}/download").status_code == 404
        assert login(stranger).get(f"/files/v/{v.pk}/preview").status_code == 404
        assert v.file_code not in login(stranger).get("/files/search", {"q": "Policy"}).content.decode()


def test_the_file_is_pale_and_labelled_for_those_who_may_see_it(office, lib):
    send("emp", lib)
    for who in ("emp", "sub"):
        html = login(who).get(f"/files/library?folder={lib.pk}").content.decode()
        assert "fm-pending" in html and "Waiting for approval" in html


def test_the_manager_is_told_and_the_link_opens_the_folder(office, lib):
    send("emp", lib)
    n = Notification.objects.get(kind=Kind.LIBRARY_REVIEW)
    assert (
        n.user_id == office["sub"].pk
        and "Policy-draft" in n.title
        and n.link == f"/files/library?folder={lib.pk}"
    )
    r = login("sub").post(f"/app/notifications/{n.pk}/open")
    assert r.status_code == 302 and r["Location"] == n.link


def test_only_the_manager_chain_and_the_boss_see_the_review_buttons(office, lib):
    f = send("emp", lib)
    assert (
        f"/files/library/files/{f.pk}/approve"
        in login("sub").get(f"/files/library?folder={lib.pk}").content.decode()
    )
    assert "/approve" not in login("emp").get(f"/files/library?folder={lib.pk}").content.decode()
    with pytest.raises(NotFound):
        library.approve(office["emp"], f.pk)  # not their own file
    assert login("other").post(f"/files/library/files/{f.pk}/approve").status_code in (302, 404)
    f.refresh_from_db()
    assert f.library_state == LibraryState.PENDING


def test_approval_publishes_to_everyone_and_tells_the_sender(office, lib):
    f = send("emp", lib)
    login("sub").post(f"/files/library/files/{f.pk}/approve")
    f.refresh_from_db()
    assert f.library_state == LibraryState.APPROVED
    page = login("other").get(f"/files/library?folder={lib.pk}").content.decode()
    assert f.display_name in page and "fm-pending" not in page
    assert login("other").get(f"/files/v/{f.current_version.pk}/download").status_code in (200, 503)
    assert Notification.objects.filter(kind=Kind.LIBRARY_DECISION, user=office["emp"]).exists()
    assert LibraryReview.objects.filter(file=f, decision=LibraryState.APPROVED).count() == 1
    assert AuditLog.objects.filter(action="library.approved").exists()


def test_rejection_needs_a_reason_hides_the_file_and_keeps_it_stored(office, lib):
    f = send("emp", lib)
    with pytest.raises(ValidationFailed):
        library.reject(office["sub"], f.pk, "no")
    login("sub").post(f"/files/library/files/{f.pk}/reject", {"reason": "Wrong year, please redo it"})
    f.refresh_from_db()
    assert f.library_state == LibraryState.REJECTED
    for who in ("emp", "sub", "other"):
        assert f.display_name not in login(who).get(f"/files/library?folder={lib.pk}").content.decode()
        assert login(who).get(f"/files/v/{f.current_version.pk}/download").status_code == 404
    n = Notification.objects.get(kind=Kind.LIBRARY_DECISION, user=office["emp"])
    assert "not approved" in n.title and "Wrong year" in n.body
    assert File.objects.filter(pk=f.pk).exists() and f.current_version is not None  # nothing erased
    with pytest.raises(NotFound):
        library.approve(office["sa"], f.pk)  # already decided


def test_a_folder_that_stops_staff_uploads_refuses_them(office, lib):
    library.set_employee_upload(office["sa"], lib.pk, False)
    r = start(
        login("emp"), DATA, uuid.uuid4(), name="2026-11-17_X.txt", scope="library", folder_id=str(lib.pk)
    )
    assert r.status_code == 403
    page = login("emp").get(f"/files/library?folder={lib.pk}").content.decode()
    assert "Create or Upload" in page and "disabled" in page
    with pytest.raises(PermissionDenied):
        library.set_employee_upload(office["emp"], lib.pk, True)


def test_staff_cannot_upload_to_the_library_root_make_folders_or_add_versions(office, lib):
    c = login("emp")
    assert start(c, DATA, uuid.uuid4(), name="2026-11-17_X.txt", scope="library").status_code == 403
    r = start(c, DATA, uuid.uuid4(), name="2026-11-17_Y.txt", scope="library", folder_id=str(lib.pk),
              relative_path="NewDir/2026-11-17_Y.txt")  # fmt: skip
    assert r.status_code == 403
    page = c.get(f"/files/library?folder={lib.pk}").content.decode()
    assert "Upload folder" not in page and ">New folder<" not in page
    official = send("boss", lib, name="2026-11-17_Official.txt", data=DATA + b"2")
    r = start(c, DATA + b"3", uuid.uuid4(), name="2026-11-17_Official.txt", scope="library",
              folder_id=str(lib.pk), file_id=str(official.pk))  # fmt: skip
    assert r.status_code == 403


def test_hr_cannot_upload_to_the_library(office, lib):
    from ots.accounts.models import Role

    from tests.accounts.factories import make_user

    make_user(Role.HR, reports_to=office["sa"], login_id="hrlib")
    r = start(
        login("hrlib"), DATA, uuid.uuid4(), name="2026-11-17_X.txt", scope="library", folder_id=str(lib.pk)
    )
    assert r.status_code in (403, 404)


def test_with_no_active_manager_the_super_admin_is_told(office):
    from types import SimpleNamespace

    gone = SimpleNamespace(is_active=False, reports_to=None)
    assert [u.pk for u in library.approvers_of(SimpleNamespace(reports_to=gone))] == [office["sa"].pk]
    assert [u.pk for u in library.approvers_of(office["emp"])] == [office["sub"].pk]  # normally the manager
