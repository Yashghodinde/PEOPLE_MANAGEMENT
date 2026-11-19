"""Office Chat writes (IMP-16.8 to 16.12). Every function checks membership on the server; a conversation
that does not exist and one the person is not in answer the same 404. A message is saved before anything is
announced, and the announcement (a live event, a bell entry) carries ids only."""

from __future__ import annotations

import collections
import time
from typing import Any

from django.db import IntegrityError, transaction
from django.db.models import Q

from ots.accounts.models import User
from ots.audit import api as audit
from ots.core.clock import now
from ots.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from ots.live.publisher import send as live_send
from ots.notifications.models import Kind as NoteKind
from ots.notifications.models import Notification

from .models import ChatAttachment, Conversation, Kind, Member, MemberRole, Message, MessageKind, Reaction

BODY_MAX = 4000
TITLE_MAX = 120
EDIT_WINDOW_SECONDS = 15 * 60
GROUP_MAX = 200
REACTIONS = ("👍", "❤️", "😂", "🎉", "🙏", "👀")
RATE_PER_MINUTE = 40

_sent: dict[Any, collections.deque[float]] = collections.defaultdict(collections.deque)


def _rate_limit(user: Any) -> None:
    """A person cannot flood a conversation: at most RATE_PER_MINUTE sends a minute."""
    q, t = _sent[user.pk], time.monotonic()
    while q and t - q[0] > 60:
        q.popleft()
    if len(q) >= RATE_PER_MINUTE:
        raise ValidationFailed("You are sending too fast. Wait a moment.")
    q.append(t)


def _active(user: Any) -> bool:
    return user is not None and getattr(user, "is_authenticated", False) and getattr(user, "is_active", False)


def membership(user: Any, conversation_id: Any) -> Member:
    """The person's active membership, or a 404. The one door every chat operation goes through."""
    m = (
        Member.objects.select_related("conversation")
        .filter(conversation_id=conversation_id, user=user, left_at__isnull=True)
        .first()
        if _active(user)
        else None
    )
    if m is None:
        raise NotFound("Not found.")
    return m


def _person(user_id: Any, company: Any) -> User:
    u = User.objects.filter(pk=user_id, is_active=True, company=company).first()
    if u is None:
        raise ValidationFailed("That person cannot be added.", fields={"people": ["Invalid."]})
    return u


def _pair_key(a: Any, b: Any) -> str:
    x, y = sorted([str(a), str(b)])
    return f"{x}:{y}"


@transaction.atomic
def open_direct(user: Any, other_id: Any) -> Conversation:
    """The one private chat between two people: opened if it exists, made if it does not."""
    if not _active(user):
        raise PermissionDenied("Sign in.", action="chat.use")
    other = _person(other_id, user.company)
    if other.pk == user.pk:
        raise ValidationFailed("Choose someone else.", fields={"people": ["Invalid."]})
    key = _pair_key(user.pk, other.pk)
    conv = Conversation.objects.filter(direct_key=key).first()
    if conv is None:
        try:
            with transaction.atomic():
                conv = Conversation.objects.create(kind=Kind.DIRECT, direct_key=key, created_by=user)
        except IntegrityError:  # both people started it at the same moment: one row won
            conv = Conversation.objects.get(direct_key=key)
        else:
            for u in (user, other):
                Member.objects.create(conversation=conv, user=u, role=MemberRole.MEMBER)
            audit.record("chat.direct_created", user, conv, {"with": str(other.pk)})
    for u in (
        user,
        other,
    ):  # someone who had left an old pair chat is back in it (a direct chat has no leaving)
        if not Member.objects.filter(conversation=conv, user=u, left_at__isnull=True).exists():
            Member.objects.create(conversation=conv, user=u, role=MemberRole.MEMBER)
    return conv


@transaction.atomic
def create_group(user: Any, title: str, people_ids: list[Any]) -> Conversation:
    title = " ".join((title or "").split())[:TITLE_MAX]
    if not title:
        raise ValidationFailed("Give the group a name.", fields={"title": ["Required."]})
    ids = {str(p) for p in people_ids} - {str(user.pk)}
    if not ids:
        raise ValidationFailed("Add at least one person.", fields={"people": ["Required."]})
    if len(ids) + 1 > GROUP_MAX:
        raise ValidationFailed("That is too many people for one group.")
    members = [_person(i, user.company) for i in ids]
    conv = Conversation.objects.create(kind=Kind.GROUP, title=title, created_by=user)
    Member.objects.create(conversation=conv, user=user, role=MemberRole.OWNER)
    for u in members:
        Member.objects.create(conversation=conv, user=u, role=MemberRole.MEMBER)
    _system(conv, user, f"{user.full_name} created the group")
    audit.record("chat.group_created", user, conv, {"title": title, "members": len(members) + 1})
    _announce(conv, user, "member")
    return conv


def _audience(conv: Conversation, *, exclude: Any = None) -> list[str]:
    qs = Member.objects.filter(conversation=conv, left_at__isnull=True)
    if exclude is not None:
        qs = qs.exclude(user=exclude)
    return [str(u) for u in qs.values_list("user_id", flat=True)]


def _audience_ids(conversation_id: Any, exclude: Any = None) -> list[str]:
    qs = Member.objects.filter(conversation_id=conversation_id, left_at__isnull=True)
    if exclude is not None:
        qs = qs.exclude(user=exclude)
    return [str(u) for u in qs.values_list("user_id", flat=True)]


def _announce(conv: Conversation, actor: Any, what: str, message_id: Any = None) -> None:
    """Tell open pages something changed. Ids only; sent when the transaction commits."""
    live_send(
        users=_audience(conv), type_="chat", conversation_id=conv.pk, what=what, message_id=message_id or ""
    )


def _system(conv: Conversation, actor: Any, text: str) -> Message:
    m = Message.objects.create(conversation=conv, sender=actor, kind=MessageKind.SYSTEM, body=text)
    Conversation.objects.filter(pk=conv.pk).update(last_message_at=m.created_at)
    return m


def _clean_body(body: str) -> str:
    text = (body or "").replace("\r\n", "\n").strip()
    if len(text) > BODY_MAX:
        raise ValidationFailed(
            f"Messages can be up to {BODY_MAX} characters.", fields={"body": ["Too long."]}
        )
    return text


def attachable_version(user: Any, version_id: Any) -> Any:
    """A file the person may put in a conversation: one they just uploaded for chat, one of their own private
    files, or a document of a File ID they belong to. Never anyone else's private file (D-107)."""
    from ots.files.models import FileVersion, FileVisibility
    from ots.workspaces.models import FileIdDocument
    from ots.workspaces.selectors import workspaces_visible_to

    v = FileVersion.objects.select_related("file").filter(pk=version_id, is_voice=False).first()
    if v is None:
        raise ValidationFailed("That file cannot be sent.", fields={"files": ["Invalid."]})
    f = v.file
    if f.owner_id == user.pk and f.visibility in (FileVisibility.CHAT, FileVisibility.PRIVATE):
        return v
    in_my_file_id = FileIdDocument.objects.filter(
        file=f, removed_at__isnull=True, workspace__in=workspaces_visible_to(user).values("pk")
    ).exists()
    if in_my_file_id:
        return v
    raise ValidationFailed("That file cannot be sent.", fields={"files": ["Invalid."]})


@transaction.atomic
def send(
    user: Any, conversation_id: Any, body: str = "", *, client_id: Any = None, reply_to: Any = None,
    kind: str = MessageKind.TEXT, version_ids: list[Any] | None = None,
) -> tuple[Message, bool]:  # fmt: skip
    """Save one message. Returns (message, created). The same client_id again returns the first message."""
    mem = membership(user, conversation_id)
    if client_id is not None:
        again = Message.objects.filter(sender=user, client_id=client_id).first()
        if again is not None:
            return again, False
    _rate_limit(user)
    text = _clean_body(body)
    version_ids = version_ids or []
    if not text and not version_ids:
        raise ValidationFailed("Write a message.", fields={"body": ["Required."]})
    attachable = [attachable_version(user, vid) for vid in version_ids]
    parent = None
    if reply_to:
        parent = Message.objects.filter(pk=reply_to, conversation=mem.conversation).first()
        if parent is None:
            raise ValidationFailed(
                "That message is not in this conversation.", fields={"reply_to": ["Invalid."]}
            )
    try:
        with transaction.atomic():
            m = Message.objects.create(
                conversation=mem.conversation, sender=user, client_id=client_id, kind=kind, body=text,
                reply_to=parent,
            )  # fmt: skip
    except IntegrityError:  # a double send at the same instant: the other one won
        return Message.objects.get(sender=user, client_id=client_id), False
    for v in attachable:
        ChatAttachment.objects.create(message=m, file=v.file, version=v)
    Conversation.objects.filter(pk=mem.conversation_id).update(last_message_at=m.created_at)
    Member.objects.filter(pk=mem.pk).update(last_read=m)  # what you send you have read
    _notify_others(mem.conversation, user, m)
    _announce(mem.conversation, user, "message", m.pk)
    return m, True


def _notify_others(conv: Conversation, sender: Any, m: Message) -> None:
    """One bell entry per unread conversation (not one per message); muted conversations make none."""
    link = f"/app/chat/c/{conv.pk}"
    who = sender.full_name if conv.kind == Kind.DIRECT else f"{sender.full_name} in {conv.title}"
    if m.kind == MessageKind.FILE:
        preview = "Sent a file"
    elif m.kind == MessageKind.VOICE:
        preview = "Sent a voice message"
    else:
        preview = " ".join(m.body.split())[:80]
    for mem in Member.objects.filter(conversation=conv, left_at__isnull=True, muted=False).exclude(
        user=sender
    ):
        open_note = Notification.objects.filter(
            user_id=mem.user_id, kind=NoteKind.CHAT_MESSAGE, link=link, read_at__isnull=True
        ).first()
        if open_note is not None:
            open_note.title, open_note.body, open_note.created_at = who[:200], preview, now()
            open_note.save(update_fields=["title", "body", "created_at"])
        else:
            Notification.objects.create(
                user_id=mem.user_id, kind=NoteKind.CHAT_MESSAGE, task=None, title=who[:200],
                body=preview[:200], link=link,
            )  # fmt: skip
    live_send(users=_audience(conv, exclude=sender), type_="notification")


@transaction.atomic
def mark_read(user: Any, conversation_id: Any, upto: Any = None) -> Member:
    """Move the person's read cursor to the newest message (or `upto`). Clears the bell entry for the chat."""
    mem = membership(user, conversation_id)
    qs = visible_messages(mem)
    target = qs.filter(pk=upto).first() if upto else qs.order_by("-id").first()
    if target is not None and (mem.last_read_id is None or target.pk > mem.last_read_id):
        Member.objects.filter(pk=mem.pk).update(last_read=target)
        Member.objects.filter(pk=mem.pk, last_delivered__isnull=True).update(last_delivered=target)
        live_send(
            users=_audience(mem.conversation, exclude=user),
            type_="chat",
            conversation_id=conversation_id,
            what="read",
            message_id=target.pk,
        )
    Notification.objects.filter(
        user=user, kind=NoteKind.CHAT_MESSAGE, link=f"/app/chat/c/{conversation_id}", read_at__isnull=True
    ).update(read_at=now())
    return mem


def visible_messages(mem: Member) -> Any:
    """What this member may see: everything since their history point (D-108)."""
    return Message.objects.filter(conversation_id=mem.conversation_id, created_at__gte=mem.history_from)


@transaction.atomic
def react(user: Any, message_id: Any, emoji: str) -> bool:
    """Toggle a reaction. Returns True when it is now on."""
    if emoji not in REACTIONS:
        raise ValidationFailed("Choose one of the reactions.", fields={"emoji": ["Invalid."]})
    m = Message.objects.select_related("conversation").filter(pk=message_id).first()
    if m is None:
        raise NotFound("Not found.")
    mem = membership(user, m.conversation_id)
    if m.created_at < mem.history_from or m.deleted_at is not None:
        raise NotFound("Not found.")
    existing = Reaction.objects.filter(message=m, user=user, emoji=emoji).first()
    if existing is not None:
        existing.delete()
        on = False
    else:
        Reaction.objects.create(message=m, user=user, emoji=emoji)
        on = True
    _announce(m.conversation, user, "reaction", m.pk)
    return on


@transaction.atomic
def edit(user: Any, message_id: Any, body: str) -> Message:
    m = (
        Message.objects.select_related("conversation")
        .select_for_update(of=("self",))
        .filter(pk=message_id)
        .first()
    )
    if m is None or m.sender_id != user.pk:
        raise NotFound("Not found.")  # nobody rewrites someone else's message
    membership(user, m.conversation_id)
    if m.kind != MessageKind.TEXT or m.deleted_at is not None:
        raise ValidationFailed("That message cannot be edited.")
    if (now() - m.created_at).total_seconds() > EDIT_WINDOW_SECONDS:
        raise ValidationFailed("A message can be edited for 15 minutes after it is sent.")
    text = _clean_body(body)
    if not text:
        raise ValidationFailed("Write a message.", fields={"body": ["Required."]})
    old = m.body
    m.body, m.edited_at = text, now()
    m.save(update_fields=["body", "edited_at"])
    audit.record(
        "chat.message_edited", user, m, {"conversation": str(m.conversation_id), "was_length": len(old)}
    )
    _announce(m.conversation, user, "edit", m.pk)
    return m


@transaction.atomic
def delete(user: Any, message_id: Any) -> Message:
    """The sender removes their own message: the text is blanked and marked, never hard-deleted (D-109)."""
    m = (
        Message.objects.select_related("conversation")
        .select_for_update(of=("self",))
        .filter(pk=message_id)
        .first()
    )
    if m is None or m.sender_id != user.pk:
        raise NotFound("Not found.")
    membership(user, m.conversation_id)
    if m.deleted_at is None:
        audit.record(
            "chat.message_deleted", user, m, {"conversation": str(m.conversation_id), "length": len(m.body)}
        )
        m.body, m.deleted_at = "", now()
        m.save(update_fields=["body", "deleted_at"])
        _announce(m.conversation, user, "delete", m.pk)
    return m


# ---- groups ------------------------------------------------------------------------------------


def _group_admin(user: Any, conversation_id: Any) -> Member:
    mem = membership(user, conversation_id)
    if mem.conversation.kind != Kind.GROUP:
        raise ValidationFailed("That is not a group.")
    if mem.role not in (MemberRole.OWNER, MemberRole.ADMIN):
        raise PermissionDenied("Only a group admin can do that.", action="chat.admin")
    return mem


@transaction.atomic
def add_members(
    user: Any, conversation_id: Any, people_ids: list[Any], *, share_history: bool = False
) -> list[Member]:
    mem = _group_admin(user, conversation_id)
    conv, added = mem.conversation, []
    active = set(
        Member.objects.filter(conversation=conv, left_at__isnull=True).values_list("user_id", flat=True)
    )
    if len(active) + len(people_ids) > GROUP_MAX:
        raise ValidationFailed("That is too many people for one group.")
    for pid in {str(p) for p in people_ids}:
        u = _person(pid, user.company)
        if u.pk in active:
            continue
        first = (
            Message.objects.filter(conversation=conv)
            .order_by("id")
            .values_list("created_at", flat=True)
            .first()
        )
        since = first if (share_history and first) else now()
        added.append(Member.objects.create(conversation=conv, user=u, history_from=since))
        _system(conv, user, f"{user.full_name} added {u.full_name}")
        audit.record("chat.member_added", user, conv, {"user": str(u.pk), "history": share_history})
    if added:
        _announce(conv, user, "member")
    return added


@transaction.atomic
def remove_member(user: Any, conversation_id: Any, target_id: Any) -> None:
    """An admin removes someone, or a person leaves. Future access ends; what was said stays stored."""
    mem = membership(user, conversation_id)
    if mem.conversation.kind != Kind.GROUP:
        raise ValidationFailed("A private chat cannot be left.")
    leaving_self = str(target_id) == str(user.pk)
    if not leaving_self:
        _group_admin(user, conversation_id)
    target = Member.objects.filter(
        conversation_id=conversation_id, user_id=target_id, left_at__isnull=True
    ).first()
    if target is None:
        raise NotFound("Not found.")
    if target.role == MemberRole.OWNER and not leaving_self:
        raise PermissionDenied("The owner cannot be removed.", action="chat.admin")
    audience = _audience(mem.conversation)  # include the person who is leaving: their page must update too
    Member.objects.filter(pk=target.pk).update(left_at=now())
    if (
        target.role == MemberRole.OWNER
    ):  # the group goes on: the longest-standing admin or member becomes owner
        nxt = (
            Member.objects.filter(conversation_id=conversation_id, left_at__isnull=True)
            .order_by("-role", "joined_at")
            .first()
        )
        if nxt is not None:
            Member.objects.filter(pk=nxt.pk).update(role=MemberRole.OWNER)
    who = target.user.full_name
    _system(mem.conversation, user, f"{who} left" if leaving_self else f"{user.full_name} removed {who}")
    audit.record(
        "chat.member_removed", user, mem.conversation, {"user": str(target.user_id), "self": leaving_self}
    )
    live_send(users=audience, type_="chat", conversation_id=conversation_id, what="member", message_id="")


@transaction.atomic
def rename_group(user: Any, conversation_id: Any, title: str) -> Conversation:
    mem = _group_admin(user, conversation_id)
    title = " ".join((title or "").split())[:TITLE_MAX]
    if not title:
        raise ValidationFailed("Give the group a name.", fields={"title": ["Required."]})
    old = mem.conversation.title
    Conversation.objects.filter(pk=mem.conversation_id).update(title=title)
    mem.conversation.title = title
    _system(mem.conversation, user, f"{user.full_name} renamed the group to {title}")
    audit.record("chat.group_renamed", user, mem.conversation, {"from": old, "to": title})
    _announce(mem.conversation, user, "member")
    return mem.conversation


@transaction.atomic
def set_role(user: Any, conversation_id: Any, target_id: Any, role: str) -> Member:
    mem = _group_admin(user, conversation_id)
    if mem.role != MemberRole.OWNER:
        raise PermissionDenied("Only the owner can change roles.", action="chat.admin")
    if role not in (MemberRole.ADMIN, MemberRole.MEMBER):
        raise ValidationFailed("Choose a role.", fields={"role": ["Invalid."]})
    target = Member.objects.filter(
        conversation_id=conversation_id, user_id=target_id, left_at__isnull=True
    ).first()
    if target is None or target.role == MemberRole.OWNER:
        raise NotFound("Not found.")
    Member.objects.filter(pk=target.pk).update(role=role)
    audit.record("chat.role_changed", user, mem.conversation, {"user": str(target_id), "role": role})
    return target


# ---- personal settings -----------------------------------------------------------------------------


def set_muted(user: Any, conversation_id: Any, muted: bool) -> None:
    mem = membership(user, conversation_id)
    Member.objects.filter(pk=mem.pk).update(muted=muted)


def set_pinned(user: Any, conversation_id: Any, pinned: bool) -> None:
    mem = membership(user, conversation_id)
    Member.objects.filter(pk=mem.pk).update(pinned_at=now() if pinned else None)


def archive(user: Any, conversation_id: Any, archived: bool = True) -> None:
    mem = membership(user, conversation_id)
    if mem.conversation.kind == Kind.GROUP and mem.role not in (MemberRole.OWNER, MemberRole.ADMIN):
        raise PermissionDenied("Only a group admin can archive a group.", action="chat.admin")
    Conversation.objects.filter(pk=mem.conversation_id).update(archived=archived)
    audit.record("chat.archived" if archived else "chat.unarchived", user, mem.conversation, {})


__all__ = ["Conflict", "Q"]
