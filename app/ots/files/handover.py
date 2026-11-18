"""Handovers (M08 section 6): send a file version to named people, and note when each first opens it."""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Iterable
from typing import Any

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.http import HttpRequest

from ots.audit import api as audit
from ots.core import events
from ots.core.clock import now
from ots.core.errors import NotFound, PermissionDenied, ValidationFailed
from ots.core.permissions import require
from ots.notifications import services as notifications
from ots.notifications.models import Kind
from ots.tasks.models import Task, TaskEvent

from . import selectors
from .models import FileHandover, FileHandoverRecipient, FileVersion, TaskFile

MAX_RECIPIENTS = 50
MAX_NOTE = 1000


def send(
    sender: Any,
    version_id: Any,
    recipient_ids: Iterable[Any],
    note: str = "",
    request: HttpRequest | None = None,
) -> FileHandover:
    """One handover, one row per recipient, one event, one bell each. All or nothing."""
    version = (
        selectors.versions_visible_to(sender)
        .filter(is_voice=False)
        .select_related("file")
        .filter(pk=version_id)
        .first()
    )  # a recording cannot be sent on as a file
    if version is None:
        raise NotFound("File not found.")
    require(sender, "file.handover", version)
    try:  # one spelling per person: capitals, braces or missing hyphens must not matter
        wanted = {str(uuid.UUID(str(r))) for r in recipient_ids}
    except ValueError:
        raise ValidationFailed("That person is not valid.", fields={"recipients": ["Invalid."]}) from None
    if not wanted:
        raise ValidationFailed("Choose at least one person.", fields={"recipients": ["Required."]})
    if len(wanted) > MAX_RECIPIENTS:
        raise ValidationFailed("Too many people.", fields={"recipients": ["Too many."]})
    note = (note or "").strip()
    if len(note) > MAX_NOTE:
        raise ValidationFailed("The note is too long.", fields={"note": ["Too long."]})
    try:
        targets = list(selectors.handover_targets(sender).filter(pk__in=wanted))
    except (ValueError, TypeError, DjangoValidationError):
        raise ValidationFailed("That person is not valid.", fields={"recipients": ["Invalid."]}) from None
    if {str(u.pk) for u in targets} != wanted:  # anyone outside the allowed set refuses the whole send
        raise PermissionDenied("You cannot send a file to one of those people.")
    link = TaskFile.objects.filter(version=version).select_related("task").order_by("-linked_at").first()
    task: Task | None = link.task if link else None
    with transaction.atomic():
        at = now()
        recent = FileHandover.objects.filter(
            version=version, from_user=sender, note=note, sent_at__gte=at - datetime.timedelta(seconds=60)
        )  # a double click or a retry must not send it twice
        for earlier in recent.prefetch_related("recipients"):
            if {str(r.to_user_id) for r in earlier.recipients.all()} == wanted:
                return earlier
        handover = FileHandover.objects.create(
            version=version, from_user=sender, task=task, note=note, sent_at=at
        )
        FileHandoverRecipient.objects.bulk_create(
            [FileHandoverRecipient(handover=handover, to_user=u) for u in targets]
        )
        body = {
            "file_code": version.file_code,
            "version": version.version_number,
            "handover_id": str(handover.pk),
            "recipients": [str(u.pk) for u in targets],
        }
        if task is not None:
            TaskEvent.objects.create(
                task=task, actor=sender, event_type="file.handed_over", payload=body, created_at=at
            )
            notifications.notify(
                [u.pk for u in targets], Kind.FILE_HANDED, task,
                f"{sender.full_name} sent you a file", actor=sender, body=version.original_filename,
            )  # fmt: skip
        audit.record("file.handed_over", sender, version, body, request)
        events.publish("file.handed_over", ("file_version", str(version.pk)), body)
    return handover


def record_open(user: Any, version: FileVersion, request: HttpRequest | None = None) -> bool:
    """First download or preview by a recipient sets `first_opened_at` once. Returns True when it did."""
    waiting = FileHandoverRecipient.objects.filter(
        to_user=user, handover__version=version, first_opened_at__isnull=True, revoked_at__isnull=True
    )
    if not waiting.exists():  # nearly every download: one cheap read, no lock, no savepoint
        return False
    opened = False
    with transaction.atomic():
        rows = list(
            FileHandoverRecipient.objects.select_for_update()
            .filter(
                to_user=user, handover__version=version, first_opened_at__isnull=True, revoked_at__isnull=True
            )
            .select_related("handover")
        )
        at = now()
        for r in rows:
            r.first_opened_at = at
            r.save(update_fields=["first_opened_at"])
            body = {"handover_id": str(r.handover_id), "file_code": version.file_code, "user": str(user.pk)}
            if r.handover.task_id is not None:
                TaskEvent.objects.create(
                    task_id=r.handover.task_id, actor=user, event_type="file.handover_opened",
                    payload=body, created_at=at,
                )  # fmt: skip
            audit.record("file.handover_opened", user, version, body, request)
            events.publish("file.handover_opened", ("file_version", str(version.pk)), body)
            opened = True
    return opened


def shared_with(user: Any, version_id: Any) -> list[FileHandoverRecipient]:
    """Everyone this version was handed to by `user` (the Super Admin: by anyone), newest first, revoked ones
    included so the history stays readable. Anyone else gets nothing, and so cannot learn it was shared."""
    from ots.accounts.models import Role

    qs = FileHandoverRecipient.objects.filter(handover__version_id=version_id).select_related(
        "to_user", "handover__from_user", "revoked_by"
    )
    if getattr(user, "role", None) != Role.SUPER_ADMIN:
        qs = qs.filter(handover__from_user=user)
    return list(qs.order_by("-handover__sent_at", "to_user__full_name"))


def revoke(user: Any, recipient_row_id: Any, request: HttpRequest | None = None) -> FileHandoverRecipient:
    """Stop sharing with one person. The sender or the Super Admin; audited; the row stays, marked revoked."""
    from ots.accounts.models import Role

    with transaction.atomic():
        r = (
            FileHandoverRecipient.objects.select_for_update(of=("self",))
            .select_related("handover__version", "handover__from_user", "to_user")
            .filter(pk=recipient_row_id)
            .first()
        )
        allowed = r is not None and (user.role == Role.SUPER_ADMIN or r.handover.from_user_id == user.pk)
        if r is None or not allowed:
            raise NotFound("Not found.")  # someone else's share and a missing one look the same
        if r.revoked_at is not None:
            return r
        r.revoked_at, r.revoked_by = now(), user
        r.save(update_fields=["revoked_at", "revoked_by"])
        body = {
            "handover_id": str(r.handover_id),
            "to": str(r.to_user_id),
            "file_code": r.handover.version.file_code,
        }
        audit.record("file.handover_revoked", user, r.handover.version, body, request)
        events.publish("file.handover_revoked", ("file_version", str(r.handover.version_id)), body)
    return r
