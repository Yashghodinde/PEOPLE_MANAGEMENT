"""F-TK-11: a voice upload that fails or is interrupted never shows as sent and never creates a discussion
message; a retry is safe and creates exactly one message."""

import uuid

import pytest
from django.conf import settings
from ots.files.models import File, FileVersion, TaskFile, UploadSession
from ots.tasks.models import CommentKind, TaskComment

from tests.accounts.factories import make_user
from tests.files.conftest import login, put, start, upload_all

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("dirs")]
AUDIO = bytes((i * 11) % 256 for i in range(3000))


def voice(c, task, key=None, data=AUDIO, name="voice.webm", **extra):
    return start(c, data, key or uuid.uuid4(), task, name=name, relation="VOICE", **extra)


def send(c, task, version_id, seconds=23, key=None):
    return c.post(
        f"/app/tasks/{task.pk}/voice",
        {"version_id": str(version_id), "seconds": seconds},
        HTTP_IDEMPOTENCY_KEY=str(key or uuid.uuid4()),
    )


def comments(task):
    return TaskComment.objects.filter(task=task, kind=CommentKind.VOICE)


def test_an_upload_killed_half_way_creates_no_message_and_nothing_can_claim_it(office):
    c, task = login("emp"), office["task"]
    r = voice(c, task)
    assert r.status_code == 201
    uid = r.json()["upload_id"]
    assert put(c, uid, AUDIO, 0, 1023).status_code == 200  # only the first piece arrives, then the tab closes
    assert not comments(task).exists() and not FileVersion.objects.exists()
    assert send(c, task, uuid.uuid4()).status_code == 404  # nothing to attach yet
    assert not comments(task).exists()


def test_retry_with_the_same_key_finishes_the_same_upload_and_sends_exactly_one_message(office):
    c, task, key = login("emp"), office["task"], uuid.uuid4()
    first = voice(c, task, key)
    uid = first.json()["upload_id"]
    put(c, uid, AUDIO, 0, 1023)
    again = voice(c, task, key)  # the page reloaded and the same recording is offered again
    assert again.status_code == 200 and again.json()["upload_id"] == uid
    got = again.json()["received_bytes"]
    assert got == 1024
    assert put(c, uid, AUDIO, got, got + 1023).status_code == 200
    assert put(c, uid, AUDIO, got + 1024, len(AUDIO) - 1).status_code == 201
    version = FileVersion.objects.get()
    assert send(c, task, version.pk).status_code == 200
    assert send(c, task, version.pk).status_code == 200  # a retried POST, answer lost the first time
    assert send(c, task, version.pk, key=uuid.uuid4()).status_code == 200  # even with a new request key
    assert comments(task).count() == 1


def test_one_recording_makes_one_message_and_another_person_cannot_claim_it(office):
    c, task = login("emp"), office["task"]
    upload_all(c, AUDIO, uuid.uuid4(), task, name="voice.webm", relation="VOICE")
    version = FileVersion.objects.get()
    boss = login("boss")
    assert send(boss, task, version.pk).status_code == 404  # not their recording
    assert not comments(task).exists()
    assert send(c, task, version.pk).status_code == 200
    comment = comments(task).get()
    assert comment.voice_version_id == version.pk and comment.body == "Voice message (0:23)"
    assert comment.voice_seconds == 23 and comment.author_id == office["emp"].pk


def test_a_recording_cannot_be_used_on_a_different_task(office):
    from tests.tasks.factories import make_task

    c, task = login("emp"), office["task"]
    other = make_task(office["sub"], office["emp"], title="Another task")
    upload_all(c, AUDIO, uuid.uuid4(), task, name="voice.webm", relation="VOICE")
    version = FileVersion.objects.get()
    assert send(c, other, version.pk).status_code == 404
    assert not comments(other).exists()


def test_an_ordinary_upload_is_not_a_recording(office):
    c, task = login("emp"), office["task"]
    upload_all(c, AUDIO, uuid.uuid4(), task, name="2026-11-17_Sales-Report.xlsx")
    assert send(c, task, FileVersion.objects.get().pk).status_code == 404


def test_the_seconds_are_clamped_and_never_trusted_beyond_the_limit(office):
    c, task = login("emp"), office["task"]
    upload_all(c, AUDIO, uuid.uuid4(), task, name="voice.webm", relation="VOICE")
    version = FileVersion.objects.get()
    assert send(c, task, version.pk, seconds=99999).status_code == 200
    assert comments(task).get().voice_seconds == settings.OTS_VOICE_MAX_SECONDS
    assert send(login("emp"), task, version.pk, seconds="abc").status_code == 422


def test_a_voice_file_never_appears_in_the_files_panel_or_task_files(office):
    c, task = login("emp"), office["task"]
    upload_all(c, AUDIO, uuid.uuid4(), task, name="voice.webm", relation="VOICE")
    version = FileVersion.objects.get()
    send(c, task, version.pk)
    assert not TaskFile.objects.exists()
    assert "voice-T-" not in c.get(f"/app/tasks/{task.pk}/files").content.decode()
    assert "voice-T-" not in login("boss").get("/files/search").content.decode()


def test_the_server_names_the_recording_and_keeps_what_the_browser_sent(office):
    c, task = login("emp"), office["task"]
    upload_all(c, AUDIO, uuid.uuid4(), task, name="Untitled.webm", relation="VOICE", display_name="x")
    v = FileVersion.objects.get()
    assert v.original_filename == "Untitled.webm"
    assert v.display_name.startswith(f"voice-{task.task_code}-") and v.display_name.endswith(".webm")
    assert v.is_voice and v.file.visibility == "TASK" and v.state in ("STAGED", "ARCHIVED")


def test_limits_and_kinds_are_enforced_on_the_server(office, settings):
    c, task = login("emp"), office["task"]
    assert voice(c, task, name="voice.exe").status_code == 422  # not audio
    assert voice(c, task, name="notes.pdf").status_code == 422
    settings.OTS_VOICE_MAX_MB = 0  # a limit smaller than any recording
    assert voice(c, task).status_code == 422
    assert not UploadSession.objects.exists()
    r = start(c, AUDIO, uuid.uuid4(), None, name="voice.webm", relation="VOICE")
    assert r.status_code == 422  # a voice message belongs to a task


def test_only_somebody_who_may_write_on_the_task_can_start_a_recording(office):
    from ots.accounts.models import Role

    make_user(Role.EMPLOYEE, reports_to=office["sub"], login_id="stranger")
    r = voice(login("stranger"), office["task"])
    assert r.status_code == 404  # cannot even see the task
    assert not UploadSession.objects.exists() and not File.objects.exists()
