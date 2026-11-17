"""IMP-10.4: voice notes in the unread marks on cards, the bell, the discussion and the history."""

import uuid

import pytest
from ots.accounts.models import Role
from ots.files.models import FileVersion
from ots.notifications.models import Kind, Notification
from ots.tasks import cards
from ots.tasks.models import CommentKind, TaskComment, TaskEvent

from tests.accounts.factories import make_user
from tests.files.conftest import login, upload_all
from tests.tasks.factories import make_task

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("dirs")]
AUDIO = bytes((i * 5) % 256 for i in range(2000))


@pytest.fixture
def world():
    sa = make_user(Role.SUPER_ADMIN, login_id="boss", full_name="Boss Person")
    m = make_user(Role.SUB_ADMIN, reports_to=sa, login_id="mgr", full_name="Meera Manager")
    e = make_user(Role.EMPLOYEE, reports_to=m, login_id="emp", full_name="Esha Employee")
    task = make_task(m, e, title="Reconcile ledger")
    return {"sa": sa, "m": m, "e": e, "task": task}


def send_voice(who, task):
    c = login(who)
    upload_all(c, AUDIO, uuid.uuid4(), task, name="voice.webm", relation="VOICE")
    version = FileVersion.objects.order_by("-uploaded_at").first()
    r = c.post(
        f"/app/tasks/{task.pk}/voice",
        {"version_id": str(version.pk), "seconds": 12},
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
    )
    assert r.status_code == 200
    return TaskComment.objects.get(voice_version=version)


def test_a_voice_message_notifies_the_others_like_a_text_message(world):
    comment = send_voice("emp", world["task"])
    assert comment.kind == CommentKind.VOICE and comment.body == "Voice message (0:12)"
    n = Notification.objects.get(user=world["m"], kind=Kind.COMMENT)
    assert n.comment_id == comment.pk and "Voice message (0:12)" in n.body
    assert not Notification.objects.filter(user=world["e"], kind=Kind.COMMENT).exists()  # not to the sender


def test_the_card_counts_text_and_voice_separately(world):
    from ots.tasks import services

    send_voice("emp", world["task"])
    services.add_comment(world["e"], world["task"].pk, "and a typed note")
    got = cards.unread_by_assignee(world["m"])
    assert got == {world["e"].pk: {"text": 1, "voice": 1}}
    card = cards.with_unread(cards.assigned_by_me_groups(world["m"]), got)[0]
    assert (card.unread_text, card.unread_voice, card.unread) == (1, 1, 2)
    page = login("mgr").get("/app/tasks").content.decode()
    assert "1 unread message, 1 unread voice note" in page and "Unread voice notes" in page


def test_the_employee_side_sees_voice_from_the_assigner_on_the_assigners_card(world):
    send_voice("mgr", world["task"])
    assert cards.unread_by_assigner(world["e"]) == {world["m"].pk: {"text": 0, "voice": 1}}
    page = login("emp").get(f"/app/tasks/by/{world['m'].pk}?filter=all").content.decode()
    assert "1 unread voice note" in page


def test_opening_the_task_clears_the_voice_mark(world):
    send_voice("emp", world["task"])
    assert cards.unread_by_assignee(world["m"])
    login("mgr").get(f"/app/tasks/{world['task'].pk}")
    assert cards.unread_by_assignee(world["m"]) == {}


def test_the_discussion_shows_a_player_for_voice_and_text_for_text(world):
    from ots.tasks import services

    c = send_voice("emp", world["task"])
    services.add_comment(world["m"], world["task"].pk, "Thanks, listening now")
    page = login("mgr").get(f"/app/tasks/{world['task'].pk}").content.decode()
    assert 'x-data="voicePlayer"' in page and f"/files/v/{c.voice_version_id}/preview" in page
    assert page.count('x-data="voicePlayer"') == 1 and "Thanks, listening now" in page
    assert 'x-data="voiceRecorder"' in page  # whoever may write may also record
    assert 'data-max-seconds="300"' in page


def test_the_history_records_one_voice_event_and_no_file_event(world):
    send_voice("emp", world["task"])
    types = list(TaskEvent.objects.filter(task=world["task"]).values_list("event_type", flat=True))
    assert types.count("comment.voice_added") == 1
    assert "file.version_added" not in types


def test_a_person_who_cannot_write_gets_no_recorder(world):
    stranger = make_user(Role.EMPLOYEE, reports_to=world["m"], login_id="stranger")
    assert login("stranger").get(f"/app/tasks/{world['task'].pk}").status_code == 404 and stranger.pk
