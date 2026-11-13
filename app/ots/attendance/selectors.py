"""Reads for the attendance screens."""

from __future__ import annotations

import calendar
import datetime
from typing import Any

from ots.accounts.models import Role, User
from ots.core.clock import IST, now, today_local
from ots.tasks.models import TaskEvent

from . import rollup
from .models import AttendanceCorrection, Holiday, WorkSession

CELL = {"P": "present", "L": "late", "A": "absent", "WO": "off", "H": "off"}


def parse_month(raw: str | None) -> datetime.date:
    """First day of the month asked for; anything unreadable means the current month."""
    try:
        y, m = (raw or "").split("-")
        return datetime.date(int(y), int(m), 1)
    except (ValueError, TypeError):
        t = today_local()
        return t.replace(day=1)


def parse_day(raw: str | None) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(raw or "")
    except ValueError:
        return None


def month_grid(user: User, first: datetime.date) -> dict[str, Any]:
    """Weeks of cells for one person and month. Today is rolled up on demand, so it is never a day behind."""
    last = first.replace(day=calendar.monthrange(first.year, first.month)[1])
    today = today_local()
    if first <= today <= last:
        rollup.rollup(today, [user.pk])
    eff = rollup.effective([user.pk], first, last)
    holidays = {
        h.date: h.name for h in Holiday.objects.filter(company_id=user.company_id, date__range=(first, last))
    }
    weeks = []
    for week in calendar.Calendar(firstweekday=0).monthdatescalendar(first.year, first.month):
        row = []
        for d in week:
            e = eff.get((user.pk, d))
            inside = d.month == first.month
            row.append({
                "date": d, "inside": inside, "today": d == today, "future": d > today,
                "status": e["status"] if e and inside else "",
                "corrected": bool(e and e["corrected"] and inside),
                "kind": CELL.get(e["status"], "") if e and inside else "", "holiday": holidays.get(d, ""),
            })  # fmt: skip
        weeks.append(row)
    counts: dict[str, int] = {}
    for e in eff.values():
        counts[e["status"]] = counts.get(e["status"], 0) + 1
    prev_m = (first - datetime.timedelta(days=1)).replace(day=1)
    next_m = (last + datetime.timedelta(days=1)).replace(day=1)
    return {
        "weeks": weeks, "counts": counts, "first": first, "has_next": next_m <= today,
        "prev": prev_m.strftime("%Y-%m"), "next": next_m.strftime("%Y-%m"),
    }  # fmt: skip


def day_detail(user: User, day: datetime.date) -> dict[str, Any]:
    """The drawer: that day's sessions, what the person did on tasks, totals. Three queries."""
    at = now()
    sessions = list(WorkSession.objects.filter(user=user, work_date=day).order_by("login_at"))
    for s in sessions:
        s.live_active = rollup._session_active_seconds(s, at) // 60  # type: ignore[attr-defined]
    start = datetime.datetime.combine(day, datetime.time.min, IST)
    end = start + datetime.timedelta(days=1)
    events = list(
        TaskEvent.objects.filter(actor=user, created_at__gte=start, created_at__lt=end)
        .select_related("task")
        .order_by("created_at")[:50]
    )
    tasks = {e.task_id: e.task for e in events}
    corrections = list(
        AttendanceCorrection.objects.filter(user=user, date=day).select_related("requested_by")
    )
    return {
        "sessions": sessions, "events": events, "tasks": list(tasks.values()), "corrections": corrections,
        "total_minutes": sum(s.live_active for s in sessions),  # type: ignore[attr-defined]
    }  # fmt: skip


def people_for(viewer: User) -> list[User]:
    """Whose attendance this person may open: themselves, and for managers their team."""
    if viewer.role == Role.EMPLOYEE:
        return [viewer]
    return list(
        User.objects.visible_to(viewer).filter(is_active=True, is_break_glass=False).order_by("full_name")
    )


def team_today(viewer: User) -> dict[str, Any]:
    """Dashboard widget: present, late and absent today across the team, who is online and who is idle."""
    team = [u for u in people_for(viewer) if u.pk != viewer.pk]
    if not team:
        return {"rows": [], "present": 0, "late": 0, "absent": 0, "live": 0, "idle": 0}
    today = today_local()
    ids = [u.pk for u in team]
    rollup.rollup(today, ids)
    eff = rollup.effective(ids, today, today)
    open_by_user = {
        s.user_id: s
        for s in WorkSession.objects.filter(user_id__in=ids, logout_at__isnull=True).order_by("login_at")
    }
    rows = []
    for u in team:
        status = eff.get((u.pk, today), {}).get("status", "")
        s = open_by_user.get(u.pk)
        idle = bool(s and s.idle_started_at and s.active_resumed_at is None)
        rows.append({"user": u, "status": status, "live": s is not None and s.live_connected, "idle": idle,
                     "tab_only": bool(s and s.idle_mode == "TAB_ONLY"), "session": s})  # fmt: skip
    return {
        "rows": rows,
        "present": sum(1 for r in rows if r["status"] == "P"),
        "late": sum(1 for r in rows if r["status"] == "L"),
        "absent": sum(1 for r in rows if r["status"] == "A"),
        "live": sum(1 for r in rows if r["live"]),
        "idle": sum(1 for r in rows if r["idle"]),
    }


def pending_corrections() -> list[AttendanceCorrection]:
    return list(
        AttendanceCorrection.objects.filter(approved_at__isnull=True)
        .select_related("user", "requested_by")
        .order_by("created_at")[:100]
    )


def holidays() -> list[Holiday]:
    return list(Holiday.objects.order_by("-date")[:200])
