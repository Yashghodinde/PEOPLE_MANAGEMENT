"""IMP-04 foundation: who can open a file uploaded into a File ID workspace. Membership is the only door."""

import uuid

import pytest
from ots.accounts.models import Role
from ots.files import selectors
from ots.files.models import FileVersion, FileVisibility
from ots.workspaces import services as ws_services
from ots.workspaces.models import Role as WsRole

from tests.accounts.factories import make_user
from tests.files.conftest import login, upload_all

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("dirs")]
DATA = bytes((i * 11) % 256 for i in range(1300))


@pytest.fixture
def world(office):
    ws = ws_services.create_workspace(office["sub"], name="AZ Exports", category="CLIENT")
    ws_services.add_member(office["sub"], ws, office["emp"], WsRole.CONTRIBUTOR)
    upload_all(
        login("emp"), DATA, uuid.uuid4(), None, name="2026-11-17_Contract.pdf",
        scope="workspace", workspace_id=str(ws.pk),
    )  # fmt: skip
    return {**office, "ws": ws, "v": FileVersion.objects.get()}


def can_open(user, v):
    return selectors.versions_visible_to(user).filter(pk=v.pk).exists()


def test_a_workspace_upload_is_a_workspace_file_linked_by_the_upload_itself(world):
    v = world["v"]
    assert v.file.visibility == FileVisibility.WORKSPACE and v.file.owner_id == world["emp"].pk
    assert world["ws"].file_id in v.file_code  # the File ID is in the code
    assert world["ws"].documents.filter(file=v.file, removed_at__isnull=True).count() == 1


def test_members_open_it_and_the_super_admin_always_can(world):
    for who in ("emp", "sub", "sa"):
        assert can_open(world[who], world["v"]), who


def test_a_managers_team_rule_does_not_open_a_file_id(world):
    boss_of_owner = make_user(Role.SUB_ADMIN, reports_to=world["sa"], login_id="mgr3")
    # the uploader's own manager (a Sub-Admin above them) is not a member either
    other = make_user(Role.EMPLOYEE, reports_to=world["sub"], login_id="nonmember")
    assert not can_open(boss_of_owner, world["v"]) and not can_open(other, world["v"])


def test_a_removed_member_loses_access_even_to_what_they_uploaded_and_history_stays(world):
    ws_services.remove_member(world["sub"], world["ws"], world["emp"])
    assert not can_open(world["emp"], world["v"])
    assert FileVersion.objects.filter(pk=world["v"].pk, uploaded_by=world["emp"]).exists()  # nothing deleted
    assert login("emp").get(f"/files/v/{world['v'].pk}/download").status_code == 404


def test_a_viewer_can_open_but_not_contribute(world):
    from ots.core.errors import PermissionDenied

    viewer = make_user(Role.EMPLOYEE, reports_to=world["sub"], login_id="viewer")
    ws_services.add_member(world["sub"], world["ws"], viewer, WsRole.VIEWER)
    assert can_open(viewer, world["v"])
    r = login("viewer").post(
        "/files/uploads",
        {"idempotency_key": str(uuid.uuid4()), "original_filename": "x.pdf", "size": 10, "sha256": "a" * 64,
         "scope": "workspace", "workspace_id": str(world["ws"].pk)},
        content_type="application/json",
    )  # fmt: skip
    assert r.status_code == 403
    with pytest.raises(PermissionDenied):
        ws_services.link_document(viewer, world["ws"], world["v"].file)


def test_hr_and_archived_workspaces(world):
    hr = make_user(Role.HR, reports_to=world["sa"], login_id="hrx")
    assert not can_open(hr, world["v"])
    ws_services.set_status(world["sa"], world["ws"], "ARCHIVED")
    assert not can_open(world["emp"], world["v"]) and can_open(world["sa"], world["v"])
