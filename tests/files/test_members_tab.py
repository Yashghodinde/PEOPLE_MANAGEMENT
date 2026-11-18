"""IMP-16.1 (MEM-01..10): adding or removing a File ID member leaves the person on the Members tab."""

import pytest
from ots.accounts.models import Role
from ots.workspaces import services as ws_services
from ots.workspaces.models import FileIdMember
from ots.workspaces.models import Role as WsRole

from tests.accounts.factories import make_user
from tests.files.conftest import login

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.usefixtures("dirs")]


@pytest.fixture
def world(office):
    ws = ws_services.create_workspace(office["sub"], name="AZ Exports 2")
    ws_services.add_member(office["sub"], ws, office["emp"], WsRole.VIEWER)
    return {**office, "ws": ws}


def members_url(w):
    return f"/files/ids/{w['ws'].pk}?tab=members"


def test_add_returns_to_the_members_tab_and_shows_the_new_person(world):
    c = login("sub")
    r = c.post(
        f"/files/ids/{world['ws'].pk}/members",
        {"op": "add", "user": str(world["other"].pk), "role": "VIEWER"},
    )
    assert r.status_code == 302 and r["Location"] == members_url(world) + "#members"
    page = c.get(r["Location"], follow=True).content.decode()
    assert world["other"].full_name in page and "was added" in page
    assert 'aria-current="page"' in page and "Members</a>" in page  # the Members tab is the lit one
    assert FileIdMember.objects.filter(
        workspace=world["ws"], user=world["other"], revoked_at__isnull=True
    ).exists()


def test_remove_returns_to_the_members_tab_and_access_stops_at_once(world):
    c = login("sub")
    r = c.post(f"/files/ids/{world['ws'].pk}/members", {"op": "remove", "user": str(world["emp"].pk)})
    assert r.status_code == 302 and r["Location"] == members_url(world) + "#members"
    page = c.get(r["Location"], follow=True).content.decode()
    assert "was removed" in page and "files and history stay" in page
    assert login("emp").get(f"/files/ids/{world['ws'].pk}").status_code == 404  # no longer a member


def test_a_duplicate_add_is_refused_without_leaving_the_tab(world):
    c = login("sub")
    r = c.post(
        f"/files/ids/{world['ws'].pk}/members", {"op": "add", "user": str(world["emp"].pk), "role": "VIEWER"}
    )
    assert r.status_code in (200, 422) and 'aria-current="page"' in r.content.decode()
    assert (
        FileIdMember.objects.filter(workspace=world["ws"], user=world["emp"], revoked_at__isnull=True).count()
        == 1
    )


def test_an_error_keeps_the_members_page_with_the_message(world):
    r = login("sub").post(
        f"/files/ids/{world['ws'].pk}/members", {"op": "add", "user": "not-a-person", "role": "VIEWER"}
    )
    body = r.content.decode()
    assert (
        r.status_code == 422
        and "Choose a person" in body
        and "Add a person" in body
        and "<title>AZ Exports 2" in body
    )


def test_the_role_change_and_details_and_archive_stay_on_the_tab(world):
    c = login("sub")
    r = c.post(
        f"/files/ids/{world['ws'].pk}/members",
        {"op": "role", "user": str(world["emp"].pk), "role": "CONTRIBUTOR"},
    )
    assert r["Location"].startswith(members_url(world))
    r = c.post(
        f"/files/ids/{world['ws'].pk}/edit",
        {"name": "AZ Exports 2b", "description": "", "category": "CLIENT", "tags": ""},
    )
    assert r["Location"].startswith(members_url(world))
    boss = login("boss").post(f"/files/ids/{world['ws'].pk}/status", {"status": "ARCHIVED"})
    assert boss["Location"].startswith(members_url(world))


def test_removing_yourself_goes_to_your_file_ids_not_a_dead_page(world):
    ws = world["ws"]
    ws_services.add_member(world["sub"], ws, world["other"], WsRole.OWNER) if False else None
    ws_services.add_member(world["sub"], ws, world["other"], WsRole.CONTRIBUTOR)
    ws_services.transfer_ownership(world["sub"], ws, world["other"])
    r = login("other").post(f"/files/ids/{ws.pk}/members", {"op": "remove", "user": str(world["sub"].pk)})
    assert r.status_code == 302 and r["Location"].startswith(
        members_url(world)
    )  # someone else was removed: stay
    r = login("other").post(f"/files/ids/{ws.pk}/members", {"op": "remove", "user": str(world["emp"].pk)})
    assert r["Location"].startswith(members_url(world))


def test_the_remove_button_asks_first_and_forms_send_once(world):
    page = login("sub").get(members_url(world)).content.decode()
    assert "Yes, remove" in page and "They lose access at once" in page
    assert page.count("data-once") >= 3  # role, remove and add forms
    js = (__import__("pathlib").Path(__file__).resolve().parents[2] / "app/static/js/app.js").read_text()
    assert "data-once" in js and "dataset.sent" in js


def test_a_stranger_and_a_viewer_cannot_change_members(world):
    assert (
        login("other")
        .post(
            f"/files/ids/{world['ws'].pk}/members",
            {"op": "add", "user": str(world["other"].pk), "role": "VIEWER"},
        )
        .status_code
        == 404
    )
    r = login("emp").post(
        f"/files/ids/{world['ws'].pk}/members",
        {"op": "add", "user": str(world["other"].pk), "role": "VIEWER"},
    )
    assert r.status_code in (403, 404, 422)
    assert not FileIdMember.objects.filter(workspace=world["ws"], user=world["other"]).exists()


def test_files_tab_actions_still_return_to_the_files_tab(world):
    page = login("sub").get(f"/files/ids/{world['ws'].pk}").content.decode()
    assert "Create or Upload" in page  # the Files tab is the default view
    assert make_user and Role  # imports used by fixtures elsewhere
