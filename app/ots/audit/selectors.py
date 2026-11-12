"""Audit viewer reads: scoped, filtered, newest first, keyset-paged (never OFFSET)."""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass
from typing import Any

from django.db.models import Q

from ots.core.errors import ValidationFailed

from .models import AuditLog, AuditLogQuerySet
from .timeline import bigint_key, day_bounds

PAGE_SIZE = 100


@dataclass(frozen=True)
class AuditFilter:
    actor: uuid.UUID | None = None
    action_prefix: str = ""
    date_from: datetime.date | None = None
    date_to: datetime.date | None = None
    target: str = ""  # exact target id (a user id, task code, file id ...)


@dataclass(frozen=True)
class RowCursor:
    at: datetime.datetime
    id: int

    def encode(self) -> str:
        return f"{self.at.isoformat()}|{self.id}"

    @classmethod
    def decode(cls, text: str | None) -> RowCursor | None:
        if not text:
            return None
        try:
            at, row_id = text.split("|", 1)
            parsed = datetime.datetime.fromisoformat(at)
            if parsed.tzinfo is None:
                raise ValueError
            return cls(parsed, bigint_key(row_id))
        except ValueError:
            raise ValidationFailed("That page link is not valid.") from None


def filtered(viewer: Any, f: AuditFilter) -> AuditLogQuerySet:
    """Everything `viewer` may see that matches the filters, newest first."""
    qs = AuditLog.objects.visible_to(viewer)
    if f.actor is not None:
        qs = qs.filter(actor_id=f.actor)
    if f.action_prefix:
        qs = qs.filter(action__startswith=f.action_prefix)
    start, end = day_bounds(f.date_from, f.date_to)
    if start is not None:
        qs = qs.filter(at__gte=start)
    if end is not None:
        qs = qs.filter(at__lt=end)
    if f.target:
        qs = qs.filter(target_id=f.target)
    return qs.order_by("-at", "-id")  # type: ignore[no-any-return]


@dataclass(frozen=True)
class AuditPage:
    rows: list[AuditLog]
    next_cursor: RowCursor | None


def audit_page(
    viewer: Any, f: AuditFilter, cursor: RowCursor | None = None, limit: int = PAGE_SIZE
) -> AuditPage:
    qs = filtered(viewer, f)
    if cursor is not None:
        qs = qs.filter(Q(at__lt=cursor.at) | Q(at=cursor.at, id__lt=cursor.id))
    rows = list(qs[: limit + 1])
    more = len(rows) > limit
    rows = rows[:limit]
    return AuditPage(rows, RowCursor(rows[-1].at, rows[-1].id) if more else None)
