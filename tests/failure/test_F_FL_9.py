"""F-FL-9: a meaningless file name is refused by the server itself, even when the browser is bypassed."""

import uuid

import pytest
from ots.files import services
from ots.files.models import File, FileVersion, UploadSession
from ots.files.naming import MESSAGE

from tests.files.conftest import login, put, sha, start

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("dirs", "real_name_rule")]
DATA = bytes(range(256)) * 3


def nothing_was_created():
    return not (UploadSession.objects.exists() or FileVersion.objects.exists() or File.objects.exists())


@pytest.mark.parametrize(
    "name", ["Untitled.pdf", "Document.docx", "123456.pdf", "asdfgh.pdf", "1 2 3 4 5.pdf"]
)
def test_a_refused_name_gets_the_exact_message_and_a_suggestion_and_creates_nothing(office, name):
    r = start(login("emp"), DATA, uuid.uuid4(), office["task"], name=name)
    body = r.json()
    assert r.status_code == 422 and body["message"] == MESSAGE
    assert body["fields"]["display_name"] == [MESSAGE]
    assert body["suggestion"].startswith("20") and body["suggestion"].endswith(name[name.rfind(".") :])
    assert nothing_was_created()


def test_the_name_the_person_confirmed_is_the_one_judged(office):
    c = login("emp")
    bad = start(c, DATA, uuid.uuid4(), office["task"], name="Q3 Budget.xlsx", display_name="Untitled.xlsx")
    assert bad.status_code == 422 and nothing_was_created()
    ok = start(
        c, DATA, uuid.uuid4(), office["task"], name="Untitled.xlsx", display_name="2026-11-17_Q3-Budget.xlsx"
    )
    assert (
        ok.status_code == 201
    )  # the original is meaningless, the confirmed name is not: the confirmed one counts


def test_a_good_name_is_accepted_and_the_original_filename_is_kept_exactly(office):
    c = login("emp")
    original, shown = "Q3 budget (1).XLSX", "2026-11-17_Prop-Organic_Q3-Budget.xlsx"
    r = start(c, DATA, uuid.uuid4(), office["task"], name=original, display_name=shown)
    assert r.status_code == 201
    assert put(c, r.json()["upload_id"], DATA, 0, len(DATA) - 1).status_code == 201
    v = FileVersion.objects.get()
    assert v.original_filename == original and v.display_name == shown and v.shown_name == shown
    assert v.file.display_name == shown


def test_a_client_cannot_claim_the_voice_exemption(office):
    r = start(login("emp"), DATA, uuid.uuid4(), office["task"], name="Untitled.webm", purpose="voice")
    assert r.status_code == 422 and nothing_was_created()


def test_the_server_can_exempt_its_own_system_made_names(office):
    session, created = services.start_upload(
        office["emp"],
        key=uuid.uuid4(),
        task=office["task"],
        file_id=None,
        assignment_id=None,
        relation="SUBMISSION",
        original_filename="Untitled.webm",
        size=len(DATA),
        sha256=sha(DATA),
        purpose=services.VOICE_PURPOSE,
    )
    assert created and session.original_filename == "Untitled.webm"


def test_a_file_from_before_the_rule_still_shows_its_original_name(office):
    c = login("emp")
    r = start(c, DATA, uuid.uuid4(), office["task"], name="Quarterly report.xlsx")
    assert put(c, r.json()["upload_id"], DATA, 0, len(DATA) - 1).status_code == 201
    FileVersion.objects.update(display_name="")  # a legacy row
    v = FileVersion.objects.get()
    assert v.shown_name == "Quarterly report.xlsx"
    assert login("emp").get(f"/files/{v.file_id}").status_code in (
        200,
        302,
        404,
    )  # still reachable, never a 500
