"""F-TK-12: a person who cannot see the task cannot play, download or even confirm the existence of its voice
notes (404)."""

import uuid

import pytest
from ots.accounts.models import Role
from ots.files import selectors
from ots.files.models import FileVersion

from tests.accounts.factories import make_user
from tests.files.conftest import login, upload_all
from tests.tasks.factories import make_task

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("dirs")]
AUDIO = bytes((i * 13) % 256 for i in range(2500))


@pytest.fixture
def note(office):
    c, task = login("emp"), office["task"]
    upload_all(c, AUDIO, uuid.uuid4(), task, name="voice.webm", relation="VOICE")
    version = FileVersion.objects.get()
    r = c.post(
        f"/app/tasks/{task.pk}/voice",
        {"version_id": str(version.pk), "seconds": 5},
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
    )
    assert r.status_code == 200
    return version


def get(who, version, action="preview"):
    return login(who).get(f"/files/v/{version.pk}/{action}")


def test_people_who_can_see_the_task_can_play_and_download_it(office, note):
    for who in ("emp", "sub", "boss"):
        assert get(who, note).status_code == 200, who
    r = get("emp", note)
    assert r["Content-Type"] == "audio/webm"
    assert get("emp", note, "download").status_code == 200


def test_an_employee_on_another_task_gets_a_404_for_both_play_and_download(office, note):
    other = make_user(Role.EMPLOYEE, reports_to=office["sub"], login_id="other2")
    make_task(office["sub"], other, title="Something else")
    for action in ("preview", "download"):
        assert get("other2", note, action).status_code == 404, action
    assert get("other", note).status_code == 404  # the fixture's second employee, on no task


def test_the_answer_for_a_real_note_and_for_a_made_up_one_is_the_same(office, note):
    real = get("other", note)
    ghost = login("other").get(f"/files/v/{uuid.uuid4()}/preview")
    assert real.status_code == ghost.status_code == 404
    assert b"voice" not in real.content.lower()


def test_a_senders_manager_by_the_team_rule_alone_cannot_hear_it_without_the_task(office, note):
    mgr2 = make_user(Role.SUB_ADMIN, reports_to=office["sa"], login_id="mgr2")
    assert get("mgr2", note).status_code == 404
    assert not selectors.versions_visible_to(mgr2).filter(pk=note.pk).exists()


def test_hr_gets_a_404_or_403_never_the_audio(office, note):
    hr = make_user(Role.HR, reports_to=office["sa"], login_id="hrv")
    assert get("hrv", note).status_code in (403, 404) and hr.pk
    assert not selectors.versions_visible_to(hr).exists()


def test_a_voice_note_is_not_reachable_through_search_the_library_or_handover(office, note):
    for url in ("/files/search?q=voice", "/files/library"):
        for who in ("boss", "sub", "emp", "other"):
            assert "voice-T-" not in login(who).get(url).content.decode(), (who, url)
    assert login("emp").post(f"/files/v/{note.pk}/handover", {"to": str(office["sub"].pk)}).status_code == 404


def test_a_note_still_uploading_is_visible_to_nobody_but_its_task_once_sent(office):
    c, task = login("emp"), office["task"]
    upload_all(c, AUDIO, uuid.uuid4(), task, name="voice.webm", relation="VOICE")
    unsent = FileVersion.objects.get()
    for who in ("emp", "sub", "other"):
        assert get(who, unsent).status_code == 404, (
            who
        )  # not a message yet, so not playable by anyone but the boss
    assert get("boss", unsent).status_code == 200
