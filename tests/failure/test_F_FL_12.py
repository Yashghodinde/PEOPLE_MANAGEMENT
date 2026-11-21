"""F-FL-12/13/15: the File ID pages. A person outside a File ID gets the same 404 as for one that does not
exist, a Viewer cannot change anything, a removed member and an archived File ID close at once."""

import uuid

import pytest
from ots.workspaces import services as ws_services
from ots.workspaces.models import Role as WsRole

from tests.files.conftest import login

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("dirs")]


@pytest.fixture
def world(office):
    ws = ws_services.create_workspace(office["sub"], name="AZ Exports", file_id="AZ-100")
    ws_services.add_member(office["sub"], ws, office["emp"], WsRole.VIEWER)
    return {**office, "ws": ws}


def url(w, tail=""):
    return f"/files/ids/{w['ws'].pk}{tail}"


def test_an_outsider_gets_the_same_404_as_for_a_missing_id(world):
    c = login("other")
    assert c.get(url(world)).status_code == 404
    assert c.get(f"/files/ids/{uuid.uuid4()}").status_code == 404
    assert (
        c.post(
            url(world, "/members"), {"op": "add", "user": str(world["other"].pk), "role": "VIEWER"}
        ).status_code
        == 404
    )


def test_the_list_shows_only_my_file_ids(world):
    assert b"AZ-100" in login("emp").get("/files/ids").content
    assert b"AZ-100" not in login("other").get("/files/ids").content


def test_a_viewer_can_look_but_not_change(world):
    c = login("emp")
    assert c.get(url(world)).status_code == 200
    bad = c.post(url(world, "/members"), {"op": "add", "user": str(world["other"].pk), "role": "VIEWER"})
    assert bad.status_code in (403, 404)
    assert not world["ws"].members.filter(user=world["other"]).exists()
    assert c.post(url(world, "/edit"), {"name": "Hacked", "category": "CLIENT"}).status_code in (403, 404)


def test_the_owner_adds_a_person_and_removing_them_closes_access(world):
    owner = login("sub")
    r = owner.post(
        url(world, "/members"), {"op": "add", "user": str(world["other"].pk), "role": "CONTRIBUTOR"}
    )
    assert r.status_code == 302
    c2 = login("other")
    assert c2.get(url(world)).status_code == 200
    owner.post(url(world, "/members"), {"op": "remove", "user": str(world["other"].pk)})
    assert c2.get(url(world)).status_code == 404


def test_the_last_owner_cannot_be_removed_and_the_page_says_why(world):
    r = login("sub").post(url(world, "/members"), {"op": "remove", "user": str(world["sub"].pk)})
    assert r.status_code == 422 and b"owner" in r.content.lower()


def test_an_archived_file_id_is_closed_to_members_but_not_to_the_super_admin(world):
    assert login("boss").post(url(world, "/status"), {"status": "ARCHIVED"}).status_code == 302
    assert login("emp").get(url(world)).status_code == 404
    assert login("boss").get(url(world)).status_code == 200
    assert login("sub").post(url(world, "/status"), {"status": "ACTIVE"}).status_code in (403, 404)


def test_an_employee_and_hr_cannot_create_one_but_hr_opens_the_list(world):
    from ots.accounts.models import Role

    from tests.accounts.factories import make_user

    make_user(Role.HR, reports_to=world["sa"], login_id="hr", full_name="Hr Person")
    assert login("emp").post("/files/ids/new", {"name": "X"}).status_code in (403, 404)
    assert login("hr").post("/files/ids/new", {"name": "X"}).status_code in (403, 404)
    assert login("hr").get("/files/ids").status_code == 200  # HR is a normal member like an Employee
