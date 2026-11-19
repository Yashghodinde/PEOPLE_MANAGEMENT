"""Office Chat reads (IMP-16.8 to 16.12). Every query starts from the person's own memberships, so a
conversation they are not in cannot appear in a list, a count, a search result or a snippet."""

from __future__ import annotations

import dataclasses
import uuid
from typing import Any

from django.db.models import Count, OuterRef, Q, Subquery, Value
from django.db.models.functions import Coalesce

from ots.accounts.models import User

from .models import ChatAttachment, Conversation, Kind, Member, Message, MessageKind

PAGE = 40


@dataclasses.dataclass
class Row:
    """One line of the conversation list."""

    conv: Conversation
    member: Member
    title: str
    other: Any  # the other person of a private chat
    last: Message | None
    preview: str
    unread: int


def _my_members(user: Any) -> Any:
    return Member.objects.filter(user=user, left_at__isnull=True, conversation__archived=False)


_ZERO = uuid.UUID(int=0)


def _with_unread(qs: Any) -> Any:
    """Each membership gets `n_unread`: messages after its read cursor, since it began, not its own."""
    newer = (
        Message.objects.filter(
            conversation_id=OuterRef("conversation_id"), created_at__gte=OuterRef("history_from"),
            deleted_at__isnull=True, id__gt=Coalesce(OuterRef("last_read_id"), Value(_ZERO)),
        )
        .exclude(sender_id=OuterRef("user_id"))
        .values("conversation_id")
        .annotate(n=Count("pk"))
        .values("n")
    )  # fmt: skip
    return qs.annotate(n_unread=Coalesce(Subquery(newer), 0))


def conversation_rows(user: Any, kind: str = "", q: str = "", limit: int = 100) -> list[Row]:
    """The sidebar: one summary per conversation, pinned first, then most recent. No history is loaded."""
    members = _with_unread(_my_members(user)).select_related("conversation")
    if kind in (Kind.DIRECT, Kind.GROUP):
        members = members.filter(conversation__kind=kind)
    members = members.order_by("-pinned_at", "-conversation__last_message_at")[
        : limit * 3
    ]  # nulls last for pinned_at is handled below
    members = list(members)
    members.sort(key=lambda m: (m.pinned_at is None, -(m.pinned_at.timestamp() if m.pinned_at else 0),
                                -m.conversation.last_message_at.timestamp()))  # fmt: skip
    ids = [m.conversation_id for m in members]
    last_ids = dict(
        Message.objects.filter(conversation_id__in=ids)
        .order_by("conversation_id", "-id")
        .distinct("conversation_id")
        .values_list("conversation_id", "id")
    )
    lasts = {m.pk: m for m in Message.objects.filter(pk__in=last_ids.values()).select_related("sender")}
    directs = [m.conversation_id for m in members if m.conversation.kind == Kind.DIRECT]
    others = {
        row[0]: row[1]
        for row in Member.objects.filter(conversation_id__in=directs)
        .exclude(user=user)
        .select_related("user")
        .values_list("conversation_id", "user")
    }
    people = {u.pk: u for u in User.objects.filter(pk__in=set(others.values()))}
    unread = {m.pk: m.n_unread for m in members}  # counted in the same query, never by loading history
    rows: list[Row] = []
    needle = (q or "").strip().lower()
    for m in members:
        c = m.conversation
        other = people.get(others.get(c.pk)) if c.kind == Kind.DIRECT else None
        title = other.full_name if other else (c.title or "Conversation")
        last = lasts.get(last_ids.get(c.pk))
        if needle and needle not in title.lower():
            continue
        rows.append(Row(c, m, title, other, last, _preview(last, user), unread[m.pk]))
    return rows[:limit]


def _preview(last: Message | None, viewer: Any) -> str:
    if last is None:
        return "No messages yet"
    if last.deleted_at is not None:
        return "This message was deleted"
    who = "You: " if last.sender_id == viewer.pk and last.kind != MessageKind.SYSTEM else ""
    if last.kind == MessageKind.FILE:
        return f"{who}Sent a file"
    if last.kind == MessageKind.VOICE:
        return f"{who}Voice message"
    return f"{who}{' '.join(last.body.split())[:80]}"


def unread_conversation_count(user: Any) -> int:
    """The sidebar badge (D-110): conversations with something unread, muted ones left out. One query."""
    return _with_unread(_my_members(user).filter(muted=False)).filter(n_unread__gt=0).count()


def people(user: Any, q: str = "", limit: int = 20) -> list[User]:
    """Colleagues to start a chat with: active, same company, not the person themselves."""
    qs = User.objects.filter(is_active=True, company=user.company, is_break_glass=False).exclude(pk=user.pk)
    needle = (q or "").strip()
    if needle:
        qs = qs.filter(
            Q(full_name__icontains=needle)
            | Q(employee_id__icontains=needle)
            | Q(designation__icontains=needle)
        )
    return list(qs.order_by("full_name")[:limit])


def history(
    mem: Member, *, before: Any = None, after: Any = None, limit: int = PAGE
) -> tuple[list[Message], bool]:
    """A page of messages, oldest first. `before` pages back in time (a cursor), `after` is what is new."""
    qs = (
        Message.objects.filter(conversation_id=mem.conversation_id, created_at__gte=mem.history_from)
        .select_related("sender", "reply_to__sender")
        .prefetch_related("attachments__version__file", "reactions")
    )
    if after:
        return list(qs.filter(pk__gt=after).order_by("id")[: limit * 5]), False
    if before:
        qs = qs.filter(pk__lt=before)
    page = list(qs.order_by("-id")[: limit + 1])
    return page[:limit][::-1], len(page) > limit


def members_of(conv: Conversation) -> list[Member]:
    return list(
        Member.objects.filter(conversation=conv, left_at__isnull=True)
        .select_related("user")
        .order_by("joined_at")
    )


def other_read_cursor(mem: Member) -> Any:
    """For a private chat: the newest of my messages the other person has read (for the Read mark)."""
    if mem.conversation.kind != Kind.DIRECT:
        return None
    return (
        Member.objects.filter(conversation_id=mem.conversation_id, left_at__isnull=True)
        .exclude(user_id=mem.user_id)
        .values_list("last_read_id", flat=True)
        .first()
    )


def search_messages(user: Any, q: str, limit: int = 30) -> list[Message]:
    """Messages containing the words, only in conversations the person is in, only since they joined."""
    needle = (q or "").strip()
    if len(needle) < 2:
        return []
    mine = _my_members(user)
    history_start = Subquery(
        mine.filter(conversation_id=OuterRef("conversation_id")).values("history_from")[:1]
    )
    qs = (
        Message.objects.filter(conversation_id__in=mine.values("conversation_id"), deleted_at__isnull=True)
        .filter(created_at__gte=history_start)
        .filter(body__icontains=needle)
        .exclude(kind=MessageKind.SYSTEM)
        .select_related("sender", "conversation")
        .order_by("-id")[:limit]
    )
    return list(qs)


def shared_files(mem: Member, newest_first: bool = False) -> list[ChatAttachment]:
    qs = (
        ChatAttachment.objects.filter(
            message__conversation_id=mem.conversation_id,
            message__created_at__gte=mem.history_from,
            message__deleted_at__isnull=True,
        )
        .select_related("version__file", "message__sender")
        .order_by("-message_id" if newest_first else "message_id")
    )
    return list(qs[:300])


def search_files(user: Any, q: str, limit: int = 30) -> list[ChatAttachment]:
    needle = (q or "").strip()
    if len(needle) < 2:
        return []
    mine = _my_members(user)
    history_start = Subquery(
        mine.filter(conversation_id=OuterRef("message__conversation_id")).values("history_from")[:1]
    )
    return list(
        ChatAttachment.objects.filter(
            message__conversation_id__in=mine.values("conversation_id"), message__deleted_at__isnull=True
        )
        .filter(message__created_at__gte=history_start)
        .filter(file__display_name__icontains=needle)
        .select_related("version__file", "message__sender", "message__conversation")
        .order_by("-message_id")[:limit]
    )
