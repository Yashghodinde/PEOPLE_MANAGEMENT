"""F-FL-11: another person (employee, Sub-Admin, HR) cannot reach a private file by any route: list, search,
download, preview, history, a guessed file id or version id. The Super Admin can, and every read is logged."""

import uuid

import pytest
from ots.accounts.models import Role
from ots.files import selectors
from ots.files.models import FileAccessLog, FileVersion

from tests.accounts.factories import make_user
from tests.files.conftest import login, upload_all

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("dirs")]
DATA = bytes((i * 17) % 256 for i in range(1800))
NAME = "2026-11-17_Salary-review-draft.txt"


@pytest.fixture
def secret(office):
    upload_all(login("emp"), DATA, uuid.uuid4(), None, name=NAME, scope="personal")
    return FileVersion.objects.get()


OUTSIDERS = ["other", "sub", "hrx"]  # a colleague, the owner's own Sub-Admin, and HR


@pytest.fixture(autouse=True)
def _hr(office):
    make_user(Role.HR, reports_to=office["sa"], login_id="hrx")


@pytest.mark.parametrize("who", OUTSIDERS)
def test_no_route_opens_a_private_file_for_an_outsider(office, secret, who):
    c = login(who)
    want_404 = (404, 403) if who == "hrx" else (404,)
    for url in (
        f"/files/v/{secret.pk}/download", f"/files/v/{secret.pk}/preview", f"/files/{secret.file_id}/trail",
        f"/files/v/{secret.pk}/handover",
    ):  # fmt: skip
        assert c.get(url).status_code in want_404, (who, url)
    assert c.get(f"/files/v/{uuid.uuid4()}/download").status_code in want_404  # a guessed id answers the same


@pytest.mark.parametrize("who", ["other", "sub"])
def test_no_list_or_search_shows_it_to_an_outsider(office, secret, who):
    c = login(who)
    for url in ("/files/search", "/files/search?q=salary", "/files/search?q=Salary-review", "/files/mine",
                "/files/shared", "/files/library"):  # fmt: skip
        assert NAME not in c.get(url).content.decode(), (who, url)
    assert not selectors.versions_visible_to(office[who]).filter(pk=secret.pk).exists()
    assert selectors.search(office[who], "salary", limit=50, offset=0) == []


def test_it_does_not_appear_in_the_team_view_of_the_owners_manager(office, secret):
    # the Sub-Admin's team rule shows team uploads on tasks, never private files
    assert not selectors.versions_visible_to(office["sub"]).filter(pk=secret.pk).exists()
    assert login("sub").get(f"/files/{secret.file_id}/trail").status_code == 404


def test_hr_sees_none_of_it_and_only_their_own_storage(office, secret):
    hr = User_by(office, "hrx")
    assert not selectors.versions_visible_to(hr).exists()
    assert login("hrx").get("/files/mine").status_code == 200
    assert login("hrx").get(f"/files/v/{secret.pk}/download").status_code in (403, 404)
    assert login("hrx").get(f"/files/{secret.file_id}/trail").status_code in (403, 404)


def test_the_owner_and_the_super_admin_can_open_it_and_every_read_is_logged(office, secret):
    assert login("emp").get(f"/files/v/{secret.pk}/download").status_code == 200
    assert login("boss").get(f"/files/v/{secret.pk}/download").status_code == 200
    assert login("boss").get(f"/files/v/{secret.pk}/preview").status_code in (200, 415)
    users = set(FileAccessLog.objects.filter(version=secret).values_list("user__login_id", flat=True))
    assert {"emp", "boss"} <= users


def test_somebody_it_was_sent_to_can_open_it_but_nobody_else_gets_that_for_free(office, secret):
    from ots.files import handover

    assert (
        login("sub").get(f"/files/v/{secret.pk}/download").status_code == 404
    )  # being the manager is not enough
    handover.send(office["emp"], secret.pk, [office["sub"].pk])  # an Employee may send to their manager
    assert login("sub").get(f"/files/v/{secret.pk}/download").status_code == 200  # access was given to them
    assert login("other").get(f"/files/v/{secret.pk}/download").status_code == 404  # and to nobody else


def User_by(office, login_id):  # noqa: N802
    from ots.accounts.models import User

    return User.objects.get(login_id=login_id)
