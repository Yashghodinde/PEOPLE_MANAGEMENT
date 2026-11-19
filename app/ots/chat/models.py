"""Office Chat (IMP-16.8): private one-to-one and group conversations, independent of tasks. PostgreSQL is the
record of every message; the live connection only tells open pages to fetch (D-103). Nothing here follows the
reporting hierarchy: only members of a conversation can read it (D-106). Rows are never hard-deleted."""

from __future__ import annotations

from django.db import models
from django.db.models import Q

from ots.accounts.models import User
from ots.core.clock import now
from ots.core.ids import uuid7
from ots.files.models import File, FileVersion


class Kind(models.TextChoices):
    DIRECT = "DIRECT", "Direct"
    GROUP = "GROUP", "Group"


class MemberRole(models.TextChoices):
    OWNER = "OWNER", "Owner"
    ADMIN = "ADMIN", "Admin"
    MEMBER = "MEMBER", "Member"


class MessageKind(models.TextChoices):
    TEXT = "TEXT", "Text"
    FILE = "FILE", "File"
    VOICE = "VOICE", "Voice"
    SYSTEM = "SYSTEM", "System"


class Conversation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    kind = models.CharField(max_length=6, choices=Kind.choices)
    title = models.CharField(max_length=120, blank=True, default="")
    direct_key = models.CharField(
        max_length=80, blank=True, default=""
    )  # the two user ids, sorted: one chat per pair
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(default=now)
    last_message_at = models.DateTimeField(default=now)
    archived = models.BooleanField(default=False)

    class Meta:
        db_table = "chat_conversations"
        constraints = [
            models.UniqueConstraint(
                fields=["direct_key"], condition=~Q(direct_key=""), name="chat_direct_pair_uq"
            ),
        ]
        indexes = [models.Index(fields=["-last_message_at"], name="chat_conv_recent_ix")]

    def __str__(self) -> str:
        return self.title or f"{self.kind} {self.pk}"


class Member(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    conversation = models.ForeignKey(Conversation, on_delete=models.PROTECT, related_name="members")
    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="chat_memberships")
    role = models.CharField(max_length=6, choices=MemberRole.choices, default=MemberRole.MEMBER)
    joined_at = models.DateTimeField(default=now)
    history_from = models.DateTimeField(
        default=now
    )  # messages before this are not shown to this member (D-108)
    left_at = models.DateTimeField(null=True, blank=True)
    last_read = models.ForeignKey(
        "Message", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    last_delivered = models.ForeignKey(
        "Message", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    muted = models.BooleanField(default=False)
    pinned_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "chat_members"
        constraints = [
            models.UniqueConstraint(
                fields=["conversation", "user"],
                condition=Q(left_at__isnull=True),
                name="chat_one_active_member_uq",
            ),
        ]
        indexes = [models.Index(fields=["user", "left_at"], name="chat_member_user_ix")]

    def __str__(self) -> str:
        return f"{self.user_id} in {self.conversation_id}"


class Message(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)  # time-ordered: the paging cursor
    conversation = models.ForeignKey(Conversation, on_delete=models.PROTECT, related_name="messages")
    sender = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    client_id = models.UUIDField(null=True, blank=True)  # the sender's own id: a retry is the same message
    kind = models.CharField(max_length=6, choices=MessageKind.choices, default=MessageKind.TEXT)
    body = models.TextField(blank=True, default="")
    reply_to = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="replies"
    )
    created_at = models.DateTimeField(default=now)
    edited_at = models.DateTimeField(null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "chat_messages"
        constraints = [
            models.UniqueConstraint(
                fields=["sender", "client_id"], condition=Q(client_id__isnull=False), name="chat_client_id_uq"
            ),
        ]
        indexes = [models.Index(fields=["conversation", "id"], name="chat_msg_conv_ix")]

    def __str__(self) -> str:
        return f"{self.kind} {self.pk}"


class ChatAttachment(models.Model):
    """A file in a conversation: a link to an existing file version. Members may open exactly that version."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    message = models.ForeignKey(Message, on_delete=models.PROTECT, related_name="attachments")
    file = models.ForeignKey(File, on_delete=models.PROTECT, related_name="chat_links")
    version = models.ForeignKey(FileVersion, on_delete=models.PROTECT, related_name="chat_links")
    created_at = models.DateTimeField(default=now)

    class Meta:
        db_table = "chat_attachments"
        indexes = [models.Index(fields=["version"], name="chat_att_version_ix")]

    def __str__(self) -> str:
        return f"{self.version_id} in {self.message_id}"


class Reaction(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    message = models.ForeignKey(Message, on_delete=models.PROTECT, related_name="reactions")
    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    emoji = models.CharField(max_length=8)
    created_at = models.DateTimeField(default=now)

    class Meta:
        db_table = "chat_reactions"
        constraints = [models.UniqueConstraint(fields=["message", "user", "emoji"], name="chat_reaction_uq")]

    def __str__(self) -> str:
        return f"{self.emoji} on {self.message_id}"
