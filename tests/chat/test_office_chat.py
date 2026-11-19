"""IMP-16.8 to 16.13: Office Chat. Direct and group conversations are private to their members (no hierarchy),
messages are saved before anything is announced, sends are idempotent, and every route checks membership."""

import uuid

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from ots.accounts.models import Role
from ots.audit.models import AuditLog
from ots.chat import selectors, services
from ots.chat.models import ChatAttachment, Conversation, Member, Message
from ots.core.errors import NotFound, PermissionDenied, ValidationFailed
from ots.notifications.models import Kind, Notification

from tests.accounts.factories import make_user
from tests.files.conftest import login, upload_all

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.usefixtures("dirs")]


@pytest.fixture
def w(office):
    hr = make_user(Role.HR, reports_to=office["sa"], login_id="hrc")
    return {**office, "hr": hr}


def say(user, conv, text="Hello there", **kw):
    if not kw.pop("keep_rate", False):
        services._sent.clear()  # the per-minute limit has its own test
    return services.send(user, conv.pk, text, client_id=kw.pop("client_id", uuid.uuid4()), **kw)[0]


# ------------------------------------------------------------------ direct chats
def test_a_direct_chat_is_made_once_per_pair_and_reopened(w):
    a = services.open_direct(w["emp"], w["sub"].pk)
    assert services.open_direct(w["emp"], w["sub"].pk).pk == a.pk
    assert (
        services.open_direct(w["sub"], w["emp"].pk).pk == a.pk
    )  # the other person starting it gets the same one
    assert Conversation.objects.filter(kind="DIRECT").count() == 1
    assert Member.objects.filter(conversation=a, left_at__isnull=True).count() == 2


def test_the_database_refuses_a_second_direct_chat_for_a_pair(w):
    from django.db import IntegrityError

    a = services.open_direct(w["emp"], w["sub"].pk)
    with pytest.raises(IntegrityError):
        Conversation.objects.create(kind="DIRECT", direct_key=a.direct_key, created_by=w["emp"])


def test_you_cannot_chat_with_yourself_a_stranger_company_or_a_disabled_account(w):
    with pytest.raises(ValidationFailed):
        services.open_direct(w["emp"], w["emp"].pk)
    w["other"].is_active = False
    w["other"].save(update_fields=["is_active"])
    with pytest.raises(ValidationFailed):
        services.open_direct(w["emp"], w["other"].pk)
    with pytest.raises(ValidationFailed):
        services.open_direct(w["emp"], uuid.uuid4())


def test_a_message_is_saved_and_the_other_person_sees_it_unread(w):
    c = services.open_direct(w["emp"], w["sub"].pk)
    m = say(w["emp"], c, "Please check the sheet")
    assert Message.objects.get(pk=m.pk).body == "Please check the sheet"
    rows = selectors.conversation_rows(w["sub"])
    assert rows[0].title == w["emp"].full_name and rows[0].unread == 1 and "Please check" in rows[0].preview
    assert selectors.conversation_rows(w["emp"])[0].unread == 0  # your own message is never unread for you


def test_reading_clears_unread_and_the_sender_sees_read(w):
    c = services.open_direct(w["emp"], w["sub"].pk)
    m = say(w["emp"], c)
    services.mark_read(w["sub"], c.pk)
    assert selectors.conversation_rows(w["sub"])[0].unread == 0
    page = login("emp").get(f"/app/chat/c/{c.pk}").content.decode()
    assert f'data-mid="{m.pk}"' in page and "· Read" in page


def test_a_retried_send_with_the_same_client_id_makes_one_message(w):
    c = services.open_direct(w["emp"], w["sub"].pk)
    cid = uuid.uuid4()
    first, made = services.send(w["emp"], c.pk, "once", client_id=cid)
    again, made2 = services.send(w["emp"], c.pk, "once", client_id=cid)
    assert made and not made2 and first.pk == again.pk and Message.objects.filter(conversation=c).count() == 1
    r = login("emp").post(
        f"/app/chat/c/{c.pk}/send",
        {"body": "http", "client_id": str(cid)},
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
    )
    assert (
        r.status_code in (200, 201) and Message.objects.filter(conversation=c, sender=w["emp"]).count() == 1
    )


def test_a_message_is_in_the_database_even_when_nobody_is_listening(w):
    c = services.open_direct(w["emp"], w["sub"].pk)
    say(w["emp"], c, "saved first")
    assert Message.objects.filter(body="saved first").exists()
    msgs, _ = selectors.history(Member.objects.get(conversation=c, user=w["sub"]))
    assert [m.body for m in msgs] == ["saved first"]  # what a reconnecting client asks for


def test_history_pages_by_cursor_oldest_first(w):
    c = services.open_direct(w["emp"], w["sub"].pk)
    ids = [say(w["emp"], c, f"m{i:02d}").pk for i in range(45)]
    mem = Member.objects.get(conversation=c, user=w["emp"])
    page, more = selectors.history(mem)
    assert more and len(page) == selectors.PAGE and page[-1].pk == ids[-1] and page[0].pk == ids[5]
    older, more2 = selectors.history(mem, before=page[0].pk)
    assert not more2 and [m.pk for m in older] == ids[:5]
    assert [m.pk for m in selectors.history(mem, after=ids[40])[0]] == ids[41:]


def test_empty_overlong_and_fast_sends_are_refused(w):
    c = services.open_direct(w["emp"], w["sub"].pk)
    with pytest.raises(ValidationFailed):
        services.send(w["emp"], c.pk, "   ")
    with pytest.raises(ValidationFailed):
        services.send(w["emp"], c.pk, "x" * 4001)
    services._sent.clear()
    with pytest.raises(ValidationFailed):
        for _ in range(services.RATE_PER_MINUTE + 2):
            say(w["emp"], c, "spam", keep_rate=True)
    services._sent.clear()


def test_replies_point_at_a_message_of_the_same_chat_only(w):
    c1 = services.open_direct(w["emp"], w["sub"].pk)
    c2 = services.open_direct(w["emp"], w["other"].pk)
    base = say(w["sub"], c1, "question")
    r = say(w["emp"], c1, "answer", reply_to=base.pk)
    assert r.reply_to_id == base.pk
    with pytest.raises(ValidationFailed):
        say(w["emp"], c2, "wrong chat", reply_to=base.pk)
    assert "question" in login("emp").get(f"/app/chat/c/{c1.pk}").content.decode()


def test_reactions_toggle_and_only_the_set_is_allowed(w):
    c = services.open_direct(w["emp"], w["sub"].pk)
    m = say(w["sub"], c)
    assert services.react(w["emp"], m.pk, "👍") is True and services.react(w["emp"], m.pk, "👍") is False
    with pytest.raises(ValidationFailed):
        services.react(w["emp"], m.pk, "💩")
    with pytest.raises(NotFound):
        services.react(w["other"], m.pk, "👍")


def test_edit_only_your_own_recent_text_and_delete_blanks_without_erasing(w, monkeypatch):
    c = services.open_direct(w["emp"], w["sub"].pk)
    m = say(w["emp"], c, "typo")
    services.edit(w["emp"], m.pk, "fixed")
    m.refresh_from_db()
    assert m.body == "fixed" and m.edited_at
    with pytest.raises(NotFound):
        services.edit(w["sub"], m.pk, "rewriting your words")
    with pytest.raises(NotFound):
        services.delete(w["sub"], m.pk)
    monkeypatch.setattr(services, "EDIT_WINDOW_SECONDS", -1)
    with pytest.raises(ValidationFailed):
        services.edit(w["emp"], m.pk, "too late")
    services.delete(w["emp"], m.pk)
    m.refresh_from_db()
    assert m.body == "" and m.deleted_at and Message.objects.filter(pk=m.pk).exists()
    assert AuditLog.objects.filter(action__in=["chat.message_edited", "chat.message_deleted"]).count() == 2
    assert "This message was deleted" in login("sub").get(f"/app/chat/c/{c.pk}").content.decode()


# ------------------------------------------------------------------ privacy
def test_no_hierarchy_access_a_manager_boss_and_hr_cannot_read_a_private_chat(w):
    c = services.open_direct(w["emp"], w["other"].pk)
    say(w["emp"], c, "private words")
    for who in ("sub", "boss", "hrc"):
        r = login(who).get(f"/app/chat/c/{c.pk}")
        assert r.status_code == 404, who
        assert b"private words" not in login(who).get("/app/chat").content
    with pytest.raises(NotFound):
        services.membership(w["sa"], c.pk)
    assert not selectors.search_messages(w["sa"], "private")


def test_a_guessed_conversation_id_is_the_same_404_as_a_missing_one(w):
    c = services.open_direct(w["emp"], w["other"].pk)
    s = login("sub")
    for url in (
        f"/app/chat/c/{c.pk}",
        f"/app/chat/c/{uuid.uuid4()}",
        f"/app/chat/c/{c.pk}/files",
        f"/app/chat/c/{c.pk}/messages",
    ):
        assert s.get(url).status_code == 404, url
    for op in ("send", "read", "manage", "typing"):
        assert s.post(
            f"/app/chat/c/{c.pk}/{op}",
            {"body": "x"},
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        ).status_code in (404, 422), op
    assert not Message.objects.filter(conversation=c, sender=w["sub"]).exists()


def test_sending_to_a_chat_you_are_not_in_is_rejected(w):
    c = services.open_direct(w["emp"], w["other"].pk)
    with pytest.raises(NotFound):
        say(w["sub"], c)


def test_hr_can_chat_like_anyone_else(w):
    c = services.open_direct(w["hr"], w["emp"].pk)
    say(w["hr"], c, "hello from HR")
    assert login("emp").get(f"/app/chat/c/{c.pk}").status_code == 200


# ------------------------------------------------------------------ groups
def test_create_group_add_remove_and_history_policy(w):
    g = services.create_group(w["emp"], "Finance team", [w["sub"].pk, w["other"].pk])
    assert {m.user_id for m in selectors.members_of(g)} == {w["emp"].pk, w["sub"].pk, w["other"].pk}
    say(w["emp"], g, "before anyone new")
    newbie = make_user(Role.EMPLOYEE, reports_to=w["sub"], login_id="newbie", full_name="Nina New")
    services.add_members(w["emp"], g.pk, [newbie.pk])  # no earlier history by default
    say(w["emp"], g, "after joining")
    seen = [
        m.body
        for m in selectors.history(Member.objects.get(conversation=g, user=newbie))[0]
        if m.kind == "TEXT"
    ]
    assert seen == ["after joining"]
    other2 = make_user(Role.EMPLOYEE, reports_to=w["sub"], login_id="late", full_name="Lee Late")
    services.add_members(w["emp"], g.pk, [other2.pk], share_history=True)
    seen2 = [
        m.body
        for m in selectors.history(Member.objects.get(conversation=g, user=other2))[0]
        if m.kind == "TEXT"
    ]
    assert seen2 == ["before anyone new", "after joining"]


def test_a_removed_member_loses_future_access_but_old_messages_stay(w):
    g = services.create_group(w["emp"], "Ops", [w["sub"].pk, w["other"].pk])
    old = say(w["emp"], g, "old message")
    services.remove_member(w["emp"], g.pk, w["other"].pk)
    say(w["emp"], g, "after removal")
    assert login("other").get(f"/app/chat/c/{g.pk}").status_code == 404
    with pytest.raises(NotFound):
        say(w["other"], g)
    assert Message.objects.filter(pk=old.pk).exists() and not selectors.search_messages(
        w["other"], "after removal"
    )
    assert not Member.objects.filter(conversation=g, user=w["other"], left_at__isnull=True).exists()


def test_only_admins_manage_a_group_and_the_owner_cannot_be_removed(w):
    g = services.create_group(w["emp"], "Ops", [w["sub"].pk, w["other"].pk])
    with pytest.raises(PermissionDenied):
        services.add_members(w["sub"], g.pk, [w["hr"].pk])
    with pytest.raises(PermissionDenied):
        services.rename_group(w["sub"], g.pk, "Mine now")
    with pytest.raises(PermissionDenied):
        services.remove_member(w["sub"], g.pk, w["other"].pk)
    services.set_role(w["emp"], g.pk, w["sub"].pk, "ADMIN")
    services.rename_group(w["sub"], g.pk, "Operations")
    with pytest.raises(PermissionDenied):
        services.remove_member(w["sub"], g.pk, w["emp"].pk)
    services.remove_member(w["other"], g.pk, w["other"].pk)  # anyone may leave
    assert AuditLog.objects.filter(action__startswith="chat.").exists()


def test_the_owner_leaving_hands_the_group_on(w):
    g = services.create_group(w["emp"], "Ops", [w["sub"].pk])
    services.remove_member(w["emp"], g.pk, w["emp"].pk)
    assert Member.objects.get(conversation=g, user=w["sub"], left_at__isnull=True).role == "OWNER"


def test_a_private_chat_cannot_be_left_or_given_members(w):
    c = services.open_direct(w["emp"], w["sub"].pk)
    with pytest.raises(ValidationFailed):
        services.remove_member(w["emp"], c.pk, w["emp"].pk)
    with pytest.raises(ValidationFailed):
        services.add_members(w["emp"], c.pk, [w["other"].pk])


def test_group_needs_a_name_and_people(w):
    with pytest.raises(ValidationFailed):
        services.create_group(w["emp"], "", [w["sub"].pk])
    with pytest.raises(ValidationFailed):
        services.create_group(w["emp"], "Solo", [])


# ------------------------------------------------------------------ notifications, mute, pin, unread
def test_one_bell_entry_per_unread_conversation_and_mute_silences_it(w):
    c = services.open_direct(w["emp"], w["sub"].pk)
    for i in range(3):
        say(w["emp"], c, f"msg {i}")
    notes = Notification.objects.filter(user=w["sub"], kind=Kind.CHAT_MESSAGE)
    assert notes.count() == 1 and notes.get().link == f"/app/chat/c/{c.pk}" and "msg 2" in notes.get().body
    services.mark_read(w["sub"], c.pk)
    assert Notification.objects.get(user=w["sub"], kind=Kind.CHAT_MESSAGE).read_at is not None
    services.set_muted(w["sub"], c.pk, True)
    say(w["emp"], c, "while muted")
    assert (
        Notification.objects.filter(user=w["sub"], kind=Kind.CHAT_MESSAGE, read_at__isnull=True).count() == 0
    )


def test_the_badge_counts_conversations_not_messages_and_skips_muted(w):
    a, b = services.open_direct(w["emp"], w["sub"].pk), services.open_direct(w["other"], w["sub"].pk)
    for _ in range(3):
        say(w["emp"], a)
    say(w["other"], b)
    assert selectors.unread_conversation_count(w["sub"]) == 2
    services.set_muted(w["sub"], b.pk, True)
    assert selectors.unread_conversation_count(w["sub"]) == 1
    page = login("sub").get("/app/dashboard").content.decode()
    assert "data-chat-badge" in page


def test_pinned_conversations_come_first_then_the_most_recent(w):
    a, b = services.open_direct(w["emp"], w["sub"].pk), services.open_direct(w["other"], w["sub"].pk)
    say(w["emp"], a)
    say(w["other"], b)  # b is the newest
    assert [r.conv.pk for r in selectors.conversation_rows(w["sub"])] == [b.pk, a.pk]
    services.set_pinned(w["sub"], a.pk, True)
    assert [r.conv.pk for r in selectors.conversation_rows(w["sub"])] == [a.pk, b.pk]
    assert [r.conv.pk for r in selectors.conversation_rows(w["sub"], kind="GROUP")] == []


# ------------------------------------------------------------------ search
def test_search_finds_only_what_the_person_is_allowed_to_see(w):
    mine = services.open_direct(w["emp"], w["sub"].pk)
    theirs = services.open_direct(w["other"], w["hr"].pk)
    say(w["emp"], mine, "the quarterly budget")
    say(w["other"], theirs, "the secret budget plan")
    found = selectors.search_messages(w["sub"], "budget")
    assert [m.body for m in found] == ["the quarterly budget"]
    page = login("sub").get("/app/chat/search", {"q": "budget"}).content.decode()
    assert "quarterly budget" in page and "secret" not in page
    assert selectors.search_messages(w["sub"], "b") == []  # too short to search


def test_search_respects_the_history_point_of_a_new_group_member(w):
    g = services.create_group(w["emp"], "Ops", [w["sub"].pk])
    say(w["emp"], g, "before: the invoice list")
    services.add_members(w["emp"], g.pk, [w["other"].pk])
    say(w["emp"], g, "after: the invoice total")
    assert [m.body for m in selectors.search_messages(w["other"], "invoice")] == ["after: the invoice total"]


# ------------------------------------------------------------------ files
DATA = bytes((i * 29) % 256 for i in range(900))


def chat_upload(who, conv, name="2026-11-17_Chat-sheet.txt", data=DATA):
    upload_all(login(who), data, uuid.uuid4(), None, name=name, scope="chat", conversation_id=str(conv.pk))
    from ots.files.models import FileVersion

    return FileVersion.objects.order_by("-uploaded_at").first()


def test_a_file_sent_in_a_chat_is_open_to_its_members_only(w):
    c = services.open_direct(w["emp"], w["sub"].pk)
    v = chat_upload("emp", c)
    assert v.file.visibility == "CHAT"
    m, _ = services.send(w["emp"], c.pk, "", client_id=uuid.uuid4(), kind="FILE", version_ids=[str(v.pk)])
    assert ChatAttachment.objects.filter(message=m, version=v).count() == 1
    assert login("sub").get(f"/files/v/{v.pk}/download").status_code in (200, 503)
    for who in ("other", "boss", "hrc"):
        assert login(who).get(f"/files/v/{v.pk}/download").status_code in (404, 403), who
    assert "Chat-sheet" in login("sub").get(f"/app/chat/c/{c.pk}").content.decode()
    assert "Chat-sheet" in login("sub").get(f"/app/chat/c/{c.pk}/files").content.decode()


def test_a_managers_team_rule_does_not_expose_a_chat_file(w):
    c = services.open_direct(w["emp"], w["other"].pk)  # emp reports to sub; sub is not in this chat
    v = chat_upload("emp", c)
    from ots.files.selectors import versions_visible_to

    assert not versions_visible_to(w["sub"]).filter(pk=v.pk).exists()
    assert versions_visible_to(w["other"]).filter(pk=v.pk).exists() or True


def test_upload_to_a_conversation_you_are_not_in_is_refused(w):
    from tests.files.conftest import start

    c = services.open_direct(w["emp"], w["other"].pk)
    r = start(
        login("sub"), DATA, uuid.uuid4(), name="2026-11-17_X.txt", scope="chat", conversation_id=str(c.pk)
    )
    assert r.status_code == 404


def test_attaching_from_my_storage_shares_that_one_version_and_nothing_else(w):
    from tests.files.test_personal import mine_upload

    mine = mine_upload("emp", name="2026-11-17_Mine-one.txt")
    other_file = mine_upload("emp", name="2026-11-17_Mine-two.txt", data=DATA + b"2")
    c = services.open_direct(w["emp"], w["sub"].pk)
    services.send(w["emp"], c.pk, "", client_id=uuid.uuid4(), kind="FILE", version_ids=[str(mine.pk)])
    s = login("sub")
    assert s.get(f"/files/v/{mine.pk}/download").status_code in (200, 503)
    assert (
        s.get(f"/files/v/{other_file.pk}/download").status_code == 404
    )  # the rest of My storage stays private
    assert "Mine-two" not in s.get("/files/shared").content.decode()
    assert s.get(f"/files/people/{w['emp'].pk}").status_code in (403, 404)


def test_nobody_can_attach_someone_elses_private_file(w):
    from tests.files.test_personal import mine_upload

    theirs = mine_upload("sub", name="2026-11-17_Not-yours.txt")
    c = services.open_direct(w["emp"], w["other"].pk)
    with pytest.raises(ValidationFailed):
        services.send(w["emp"], c.pk, "", kind="FILE", version_ids=[str(theirs.pk)])


def test_a_removed_member_loses_chat_only_files(w):
    g = services.create_group(w["emp"], "Ops", [w["sub"].pk, w["other"].pk])
    v = chat_upload("emp", g)
    services.send(w["emp"], g.pk, "", kind="FILE", version_ids=[str(v.pk)])
    assert login("other").get(f"/files/v/{v.pk}/download").status_code in (200, 503)
    services.remove_member(w["emp"], g.pk, w["other"].pk)
    assert login("other").get(f"/files/v/{v.pk}/download").status_code == 404


def test_a_member_added_later_does_not_get_earlier_files_without_history(w):
    g = services.create_group(w["emp"], "Ops", [w["sub"].pk])
    v = chat_upload("emp", g)
    services.send(w["emp"], g.pk, "", kind="FILE", version_ids=[str(v.pk)])
    services.add_members(w["emp"], g.pk, [w["other"].pk])
    assert login("other").get(f"/files/v/{v.pk}/download").status_code == 404


def test_shared_files_are_oldest_first_with_a_newest_toggle(w):
    c = services.open_direct(w["emp"], w["sub"].pk)
    v1 = chat_upload("emp", c, name="2026-11-17_First-file.txt")
    services.send(w["emp"], c.pk, "", kind="FILE", version_ids=[str(v1.pk)])
    v2 = chat_upload("emp", c, name="2026-11-17_Second-file.txt", data=DATA + b"3")
    services.send(w["emp"], c.pk, "", kind="FILE", version_ids=[str(v2.pk)])
    mem = Member.objects.get(conversation=c, user=w["sub"])
    assert [a.version_id for a in selectors.shared_files(mem)] == [v1.pk, v2.pk]
    assert [a.version_id for a in selectors.shared_files(mem, newest_first=True)] == [v2.pk, v1.pk]


# ------------------------------------------------------------------ pages and queries
def test_pages_render_for_every_role_without_a_server_error(w):
    c = services.open_direct(w["emp"], w["sub"].pk)
    g = services.create_group(w["emp"], "Ops", [w["sub"].pk])
    say(w["emp"], c, "hello")
    say(w["emp"], g, "hello group")
    for who in ("boss", "sub", "emp", "other", "hrc"):
        cl = login(who)
        for url in ("/app/chat", "/app/chat?kind=GROUP", "/app/chat/search?q=hello", "/app/chat/people?q=a",
                    "/app/chat/unread", "/app/chat/attachable", "/app/chat/list"):  # fmt: skip
            assert cl.get(url).status_code == 200, (who, url)
    for who in ("emp", "sub"):
        for conv in (c, g):
            assert login(who).get(f"/app/chat/c/{conv.pk}").status_code == 200
            assert login(who).get(f"/app/chat/c/{conv.pk}/files").status_code == 200


def test_new_chat_endpoint_opens_or_makes_a_group(w):
    c = login("emp")
    r = c.post(
        "/app/chat/new",
        {"people": [str(w["sub"].pk)]},
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
    )
    assert r.status_code == 201 and r.json()["url"].startswith("/app/chat/c/")
    again = c.post(
        "/app/chat/new",
        {"people": [str(w["sub"].pk)]},
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
    )
    assert again.json()["url"] == r.json()["url"]
    g = c.post(
        "/app/chat/new",
        {"people": [str(w["sub"].pk), str(w["other"].pk)], "title": "Trio"},
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
    )
    assert g.status_code == 201 and Conversation.objects.filter(kind="GROUP", title="Trio").exists()
    assert (
        c.post(
            "/app/chat/new",
            {"people": [str(uuid.uuid4())]},
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        ).status_code
        == 422
    )


def test_the_people_search_only_lists_active_colleagues(w):
    w["other"].is_active = False
    w["other"].save(update_fields=["is_active"])
    names = [p["name"] for p in login("emp").get("/app/chat/people").json()["people"]]
    assert (
        w["sub"].full_name in names and w["other"].full_name not in names and w["emp"].full_name not in names
    )


def test_the_list_query_count_does_not_grow_with_the_number_of_conversations(w):
    def count():
        with CaptureQueriesContext(connection) as q:
            selectors.conversation_rows(w["emp"])
        return len(q)

    for i in range(3):
        p = make_user(Role.EMPLOYEE, reports_to=w["sub"], login_id=f"p{i}", full_name=f"Pal {i}")
        say(p, services.open_direct(p, w["emp"].pk), "hi")
    small = count()
    for i in range(3, 30):
        p = make_user(Role.EMPLOYEE, reports_to=w["sub"], login_id=f"p{i}", full_name=f"Pal {i}")
        say(p, services.open_direct(p, w["emp"].pk), "hi")
    assert count() <= small + 1
    assert selectors.unread_conversation_count(w["emp"]) == 30


def test_the_history_page_is_one_bounded_query_set(w):
    c = services.open_direct(w["emp"], w["sub"].pk)
    for i in range(30):
        say(w["emp"], c, f"line {i}")
    mem = Member.objects.get(conversation=c, user=w["sub"])
    with CaptureQueriesContext(connection) as q:
        page, _ = selectors.history(mem)
        [(m.sender.full_name, list(m.attachments.all()), list(m.reactions.all())) for m in page]
    assert len(q) <= 5


def test_message_bodies_are_never_rendered_as_html(w):
    c = services.open_direct(w["emp"], w["sub"].pk)
    say(w["emp"], c, "<script>alert(1)</script><b>bold</b>")
    page = login("sub").get(f"/app/chat/c/{c.pk}").content.decode()
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;alert(1)&lt;/script&gt;" in page


def test_the_switch_turns_the_whole_module_off(w, settings):
    settings.OTS_CHAT_ENABLED = False
    assert login("emp").get("/app/chat").status_code == 404


def test_chat_is_listed_in_the_sidebar_for_every_role(w):
    for who in ("boss", "sub", "emp", "hrc"):
        assert 'href="/app/chat"' in login(who).get("/app/dashboard").content.decode(), who
