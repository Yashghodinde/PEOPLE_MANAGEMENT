"""IMP-16.4: every Files page, opened as every kind of person, never answers 500. Runs outside the test
wrapper transaction (`transaction=True`) so a missing `transaction.atomic()` around an audit write shows up here
the way it does in the browser."""

import uuid

import pytest
from ots.accounts.models import Role
from ots.files import folders, library
from ots.workspaces import physical
from ots.workspaces import services as ws_services
from ots.workspaces.models import Role as WsRole

from tests.accounts.factories import make_user
from tests.files.conftest import login, upload_all
from tests.files.test_personal import mine_upload

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.usefixtures("dirs")]


@pytest.fixture
def seeded(office):
    make_user(Role.HR, reports_to=office["sa"], login_id="hrsweep")
    v = mine_upload("emp", name="2026-11-17_Sweep-notes.txt")
    fol = folders.create_personal(office["emp"], "Sweep folder")
    lib = library.create_folder(office["sa"], "Sweep library")
    ws = ws_services.create_workspace(office["sub"], name="Sweep Co")
    ws_services.add_member(office["sub"], ws, office["emp"], WsRole.CONTRIBUTOR)
    rec = physical.create(office["sub"], ws, name="Box", office="Mumbai", block="B1", shelf="S1")
    wf = __import__("ots.workspaces.folders", fromlist=["x"]).create(office["sub"], ws, "Sub")
    upload_all(login("emp"), b"s" * 600, uuid.uuid4(), None, name="2026-11-17_Sweep-ws.txt", scope="workspace",
               workspace_id=str(ws.pk), folder_id=str(wf.pk))  # fmt: skip
    return {**office, "v": v, "fol": fol, "lib": lib, "ws": ws, "rec": rec, "wf": wf}


def urls(s):
    ws, emp = s["ws"], s["emp"]
    return [
        "/files/mine", "/files/mine/2026-11-17", f"/files/mine/f/{s['fol'].pk}", "/files/tasks", "/files/shared",
        "/files/library", f"/files/library?folder={s['lib'].pk}", "/files/ids", "/files/ids/new", f"/files/ids/{ws.pk}",
        f"/files/ids/{ws.pk}?tab=members", f"/files/ids/{ws.pk}/f/{s['wf'].pk}", f"/files/ids/{ws.pk}/physical",
        f"/files/v/{s['v'].pk}/sharing", f"/files/{s['v'].file_id}/trail", f"/files/v/{s['v'].pk}/handover",
        "/files/search?q=sweep", "/files/deleted", "/files/people", f"/files/people/{emp.pk}",
        f"/files/people/{emp.pk}?day=2026-11-17", f"/files/people/{emp.pk}?day=not-a-date",
        "/app/tasks", "/app/tasks/all", "/app/people", f"/app/people/{emp.pk}", "/app/org", "/app/notifications",
    ]  # fmt: skip


@pytest.mark.parametrize("who", ["boss", "sub", "emp", "other", "hrsweep"])
def test_no_files_page_answers_500_for_any_role(seeded, who):
    c = login(who)
    bad = [(u, r.status_code) for u in urls(seeded) if (r := c.get(u)).status_code >= 500]
    assert not bad, bad


def test_the_super_admin_can_open_a_persons_storage_and_it_is_recorded(seeded):
    from ots.audit.models import AuditLog

    c = login("boss")
    r = c.get(f"/files/people/{seeded['emp'].pk}")
    assert r.status_code == 200 and "Sweep-notes" in r.content.decode()
    assert AuditLog.objects.filter(action="storage.browsed").exists()


def test_managers_and_employees_are_refused_not_crashed(seeded):
    for who in ("sub", "emp", "other", "hrsweep"):
        assert login(who).get(f"/files/people/{seeded['emp'].pk}").status_code in (403, 404), who


def test_a_folder_setting_change_writes_its_audit_row_inside_a_transaction(seeded):
    r = login("boss").post(f"/files/library/folders/{seeded['lib'].pk}/uploads", {"allow": "0"})
    assert r.status_code == 302
    seeded["lib"].refresh_from_db()
    assert seeded["lib"].employee_upload is False
