"""The daily roll-up and the effective status (M06 section 3)."""

from __future__ import annotations

import datetime
from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from django.conf import settings

from ots.accounts.models import User
from ots.core.clock import IST, now

from .models import AttendanceCorrection, AttendanceDay, AttendanceStatus, Holiday, WorkSession

S = AttendanceStatus


def late_grace() -> datetime.timedelta:
    return datetime.timedelta(minutes=int(getattr(settings, "OTS_LATE_GRACE_MINUTES", 10)))


def _session_active_seconds(ws: WorkSession, at: datetime.datetime) -> int:
    """Closed sessions carry their own figure; an open one counts up to now."""
    if ws.active_seconds is not None:
        return ws.active_seconds
    from .sessions import active_seconds

    return active_seconds(ws, at)


def rollup(day: datetime.date, user_ids: Iterable[Any] | None = None) -> int:
    """Compute one day for each active person and upsert it. Safe to run again. Returns rows written.

    Four queries however many people: users, holidays, that day's sessions, one upsert.
    """
    at = now()
    qs = User.objects.filter(is_active=True, is_break_glass=False)
    if user_ids is not None:
        qs = qs.filter(pk__in=list(user_ids))
    users = list(qs.only("id", "company_id", "weekly_off", "shift_start", "date_of_joining"))
    if not users:
        return 0
    holiday_companies = set(Holiday.objects.filter(date=day).values_list("company_id", flat=True))
    by_user: dict[Any, list[WorkSession]] = defaultdict(list)
    for ws in WorkSession.objects.filter(work_date=day, user_id__in=[u.pk for u in users]):
        by_user[ws.user_id].append(ws)
    rows = []
    for u in users:
        if u.date_of_joining and day < u.date_of_joining:
            continue
        sess = by_user.get(u.pk, [])
        fields: dict[str, Any] = {
            "first_login": None, "last_logout": None, "active_minutes": 0,
            "sessions": len(sess), "idle_logouts": 0, "late_minutes": 0,
        }  # fmt: skip
        if day.weekday() in (u.weekly_off or []):
            status = S.WO
        elif u.company_id in holiday_companies:
            status = S.H
        elif sess:
            first = min(s.login_at for s in sess)
            fields["first_login"] = first
            closed = [s.logout_at for s in sess if s.logout_at]
            fields["last_logout"] = max(closed) if closed and len(closed) == len(sess) else None
            fields["active_minutes"] = sum(_session_active_seconds(s, at) for s in sess) // 60
            fields["idle_logouts"] = sum(1 for s in sess if s.logout_reason == "IDLE_45")
            status = S.P
            if u.shift_start is not None:
                shift = datetime.datetime.combine(day, u.shift_start, IST)
                if first > shift + late_grace():
                    status = S.L
                fields["late_minutes"] = (
                    max(0, int((first - shift).total_seconds() // 60)) if first > shift else 0
                )
        else:
            status = S.A
        rows.append(AttendanceDay(user_id=u.pk, date=day, status=status, computed_at=at, **fields))
    AttendanceDay.objects.bulk_create(
        rows,
        update_conflicts=True,
        unique_fields=["user", "date"],
        update_fields=["status", "first_login", "last_logout", "active_minutes", "sessions", "idle_logouts",
                       "late_minutes", "computed_at"],
    )  # fmt: skip
    return len(rows)


def effective(
    user_ids: Iterable[Any], first: datetime.date, last: datetime.date
) -> dict[tuple[Any, datetime.date], dict[str, Any]]:
    """{(user, date): {status, original, corrected, row}}; the latest approved correction wins."""
    ids = list(user_ids)
    out: dict[tuple[Any, datetime.date], dict[str, Any]] = {}
    for d in AttendanceDay.objects.filter(user_id__in=ids, date__range=(first, last)):
        out[(d.user_id, d.date)] = {"status": d.status, "original": d.status, "corrected": False, "row": d}
    fixes = AttendanceCorrection.objects.filter(
        user_id__in=ids, date__range=(first, last), approved_at__isnull=False
    ).order_by("approved_at")
    for c in fixes:  # later approvals overwrite earlier ones
        cur = out.get((c.user_id, c.date), {"original": "", "row": None})
        out[(c.user_id, c.date)] = {**cur, "status": c.new_status, "corrected": True, "reason": c.reason}
    return out
