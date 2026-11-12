"""Activity timeline: one person's history merged from several sources.

Each source is a *provider*: a function that returns that source's events for one person, newest first, in a
single query. M03 registers the audit_log provider; M06 (work sessions) and M08 (file uploads, handovers)
and M07 (task events) register theirs with `register_provider`, without editing this module.

Order is total and stable: (time, source name, key), newest first. A cursor is the last event shown, so
paging never skips or repeats a row even when two events share the same instant.
"""

from __future__ import annotations

import datetime
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from django.db.models import Q
from django.db.models.query import QuerySet

from ots.core.clock import IST
from ots.core.errors import ValidationFailed

from .models import AuditLog

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


@dataclass(frozen=True)
class Event:
    at: datetime.datetime
    source: str  # which provider made it, e.g. "audit", "task", "session"
    key: Any  # unique within the source (int id or UUID); breaks ties
    title: str
    detail: str = ""
    url: str = ""

    def sort_key(self) -> tuple[datetime.datetime, str, Any]:
        return (self.at, self.source, self.key)


@dataclass(frozen=True)
class Cursor:
    """Position after the last event shown. Everything strictly older (in the total order) comes next."""

    at: datetime.datetime
    source: str
    key: str

    def encode(self) -> str:
        return f"{self.at.isoformat()}|{self.source}|{self.key}"

    @classmethod
    def decode(cls, text: str | None) -> Cursor | None:
        if not text:
            return None
        try:
            at, source, key = text.split("|", 2)
            parsed = datetime.datetime.fromisoformat(at)
            if parsed.tzinfo is None or not source or not key:
                raise ValueError
            return cls(parsed, source, key)
        except ValueError:
            raise ValidationFailed("That page link is not valid.") from None

    @classmethod
    def of(cls, event: Event) -> Cursor:
        return cls(event.at, event.source, str(event.key))

    def older_than(self, source: str, at_field: str, key_field: str, key_type: Callable[[str], Any]) -> Q:
        """Filter for one source's rows that come after this cursor in the total order."""
        if source == self.source:
            return Q(**{f"{at_field}__lt": self.at}) | Q(
                **{at_field: self.at, f"{key_field}__lt": key_type(self.key)}
            )
        if source < self.source:  # same instant: a "lower" source sorts after a "higher" one
            return Q(**{f"{at_field}__lte": self.at})
        return Q(**{f"{at_field}__lt": self.at})


# (user_id, start, end, cursor, limit) -> up to `limit` events, newest first. ONE query.
Provider = Callable[
    [Any, datetime.datetime | None, datetime.datetime | None, Cursor | None, int], list[Event]
]
_PROVIDERS: dict[str, Provider] = {}


def register_provider(name: str, fn: Provider) -> None:
    if name in _PROVIDERS:
        raise RuntimeError(f"timeline provider already registered: {name}")
    _PROVIDERS[name] = fn


def providers() -> list[str]:
    return sorted(_PROVIDERS)


@dataclass(frozen=True)
class TimelinePage:
    events: list[Event]
    next_cursor: Cursor | None


def day_bounds(
    date_from: datetime.date | None, date_to: datetime.date | None
) -> tuple[datetime.datetime | None, datetime.datetime | None]:
    """Inclusive local dates -> [start, end) instants in Asia/Kolkata."""
    start = datetime.datetime.combine(date_from, datetime.time.min, IST) if date_from else None
    end = (
        datetime.datetime.combine(date_to + datetime.timedelta(days=1), datetime.time.min, IST)
        if date_to
        else None
    )
    return start, end


def activity_timeline(
    user_id: Any,
    date_from: datetime.date | None = None,
    date_to: datetime.date | None = None,
    *,
    cursor: Cursor | None = None,
    limit: int = DEFAULT_LIMIT,
) -> TimelinePage:
    """Merge every provider's events for one person, newest first. One query per provider.

    The caller must already have checked that the viewer may see this person (`User.objects.visible_to`).
    """
    limit = max(1, min(limit, MAX_LIMIT))
    start, end = day_bounds(date_from, date_to)
    merged: list[Event] = []
    for name in sorted(_PROVIDERS):
        merged.extend(_PROVIDERS[name](user_id, start, end, cursor, limit + 1))
    merged.sort(key=Event.sort_key, reverse=True)
    page = merged[:limit]
    more = len(merged) > limit
    return TimelinePage(page, Cursor.of(page[-1]) if more and page else None)


# ---------------------------------------------------------------- audit_log provider

_AUTH_TITLES = {
    "auth.login_ok": "Signed in",
    "auth.login_failed": "Wrong password",
    "auth.login_blocked": "Sign-in refused: account locked",
    "auth.locked": "Account locked after too many wrong passwords",
    "auth.password_change_failed": "Wrong current password when changing password",
}


def _apply_window(qs: QuerySet[Any], at_field: str, start: Any, end: Any) -> QuerySet[Any]:
    if start is not None:
        qs = qs.filter(**{f"{at_field}__gte": start})
    if end is not None:
        qs = qs.filter(**{f"{at_field}__lt": end})
    return qs


def audit_provider(
    user_id: Any,
    start: datetime.datetime | None,
    end: datetime.datetime | None,
    cursor: Cursor | None,
    limit: int,
) -> list[Event]:
    """`auth.*` rows where this person is the actor or the target (their sign-ins, lockouts)."""
    qs = AuditLog.objects.filter(action__startswith="auth.").filter(
        Q(actor_id=user_id) | Q(target_type="user", target_id=str(user_id))
    )
    qs = _apply_window(qs, "at", start, end)
    if cursor is not None:
        qs = qs.filter(cursor.older_than("audit", "at", "id", int))
    rows = qs.order_by("-at", "-id")[:limit]
    return [
        Event(
            at=r.at,
            source="audit",
            key=r.id,
            title=_AUTH_TITLES.get(r.action, r.action),
            detail=f"from {r.client_ip}" if r.client_ip else "",
        )
        for r in rows
    ]


register_provider("audit", audit_provider)
