"""IMP-15.3 (D-096): File IDs look like FTS-AZE-483921."""

import re

import pytest
from ots.core.errors import ValidationFailed
from ots.workspaces import ids
from ots.workspaces import services as ws_services
from ots.workspaces.models import FileIdWorkspace

from tests.files.conftest import login

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("dirs")]


@pytest.mark.parametrize(
    "name,expected",
    [
        ("AZ Exports", "AZE"),
        ("Prop Organic Foods", "POF"),
        ("Kropbook Agritech", "KA"),
        ("Suryakriti FPC", "SF"),
        ("ABC Trading Company", "ATC"),
        ("Kropbook", "KROP"),
        ("Zed Pvt. Ltd.", "ZED"),
        ("Acme Industries Pvt Ltd", "AI"),
        ("  !!  ", "XX"),
        ("a", "AX"),
    ],
)
def test_prefix_rules(name, expected):
    assert ids.prefix(name) == expected


def test_a_generated_id_has_a_fixed_shape():
    for _ in range(50):
        assert re.fullmatch(r"FTS-[A-Z]{2,4}-\d{6}", ids.generate("AZ Exports"))
    assert ids.generate("AZ Exports").startswith("FTS-AZE-")


def test_creating_with_a_blank_id_generates_one_from_the_name(office):
    ws = ws_services.create_workspace(office["sub"], name="AZ Exports")
    assert re.fullmatch(r"FTS-AZE-\d{6}", ws.file_id) and ws.file_id_norm == ws.file_id


def test_a_collision_makes_the_server_draw_another_number(office, monkeypatch):
    first = ws_services.create_workspace(office["sub"], name="AZ Exports")
    numbers = iter([int(first.file_id[-6:]), int(first.file_id[-6:]), 123456])
    monkeypatch.setattr(ids.secrets, "randbelow", lambda n: next(numbers))
    second = ws_services.create_workspace(office["sub"], name="AZ Exports")
    assert second.file_id == "FTS-AZE-123456" and second.file_id != first.file_id


def test_renaming_never_changes_the_id(office):
    ws = ws_services.create_workspace(office["sub"], name="AZ Exports")
    before = ws.file_id
    ws_services.edit_details(
        office["sub"], ws, name="Totally Different Name", description="", category=ws.category, tags=""
    )
    ws.refresh_from_db()
    assert ws.file_id == before


def test_a_typed_id_must_follow_the_same_format_when_it_comes_from_the_form(office):
    for bad in ("AZ-100", "FTS-AZE-12", "FTS-A-123456", "FTS-AZE-ABCDEF", "fts-aze-1234567"):
        with pytest.raises(ValidationFailed):
            ws_services.create_workspace(office["sub"], name="X Co", file_id=bad, strict_format=True)
    ws = ws_services.create_workspace(
        office["sub"], name="X Co", file_id="fts-xco-654321", strict_format=True
    )
    assert ws.file_id == "FTS-XCO-654321"
    with pytest.raises(ValidationFailed):  # already registered
        ws_services.create_workspace(
            office["sub"], name="Other", file_id="FTS-XCO-654321", strict_format=True
        )


def test_older_ids_stay_valid_and_the_form_posts_through_the_view(office):
    ws_services.create_workspace(
        office["sub"], name="Old Co", file_id="AZ-100"
    )  # the service still accepts it
    c = login("sub")
    r = c.post("/files/ids/new", {"name": "New Co", "file_id": "AZ-101", "category": "CLIENT"})
    assert r.status_code == 422 and b"FTS-AZE-483921" in r.content
    r = c.post("/files/ids/new", {"name": "New Co", "file_id": "", "category": "CLIENT"})
    assert r.status_code == 302 and FileIdWorkspace.objects.filter(file_id__regex=r"^FTS-NCO-\d{6}$").exists()


def test_the_check_endpoint_suggests_and_reports_availability(office):
    c = login("sub")
    d = c.get("/files/ids/check", {"name": "AZ Exports"}).json()
    assert d["ok"] and re.fullmatch(r"FTS-AZE-\d{6}", d["id"])
    ws = ws_services.create_workspace(office["sub"], name="Taken Co")
    taken = c.get("/files/ids/check", {"file_id": ws.file_id}).json()
    assert taken["ok"] is False and "registered" in taken["message"]
    assert c.get("/files/ids/check", {"file_id": "nope"}).json()["ok"] is False
    assert login("emp").get("/files/ids/check", {"name": "x"}).status_code == 403
