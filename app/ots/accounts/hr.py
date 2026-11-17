"""What the HR dashboard shows: people and attendance, never tasks or files (IMP-08)."""

from __future__ import annotations

from typing import Any

from django.db.models import Count

from ots.attendance import selectors as attendance_selectors

from .models import Role, User


def hr_dashboard(viewer: User) -> dict[str, Any]:
    people = User.objects.visible_to(viewer)
    by_role = dict(people.filter(is_active=True).values_list("role").annotate(n=Count("pk")))
    return {
        "page_title": "Dashboard",
        "role_counts": [(label, by_role.get(value, 0)) for value, label in Role.choices],
        "active_total": sum(by_role.values()),
        "inactive_total": people.filter(is_active=False).count(),
        "team_today": attendance_selectors.team_today(viewer),
    }
