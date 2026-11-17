"""One place that says how designations rank (People directory ordering).

Higher number = listed first. Names are matched ignoring case and extra spaces.
A designation that is not in the table, and a blank one, are listed after every
configured designation (never above them). Add or change entries here, or
override the whole table with `OTS_DESIGNATION_PRIORITY` in settings; no
migration is needed.

This is used only to order the People list. The org chart follows
`reports_to`, never this table.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.db.models import Case, IntegerField, QuerySet, Value, When
from django.db.models.functions import Lower, Trim

DESIGNATION_PRIORITY: dict[str, int] = {
    "Director": 100,
    "Manager": 90,
    "Operations Head": 85,
    "Senior Accountant": 70,
    "Accounts Executive": 60,
    "Employee": 50,
    "Dispatcher": 40,
    "Clerk": 30,
}


def priority_table() -> dict[str, int]:
    table = getattr(settings, "OTS_DESIGNATION_PRIORITY", None) or DESIGNATION_PRIORITY
    return {" ".join(name.split()).lower(): rank for name, rank in table.items()}


def priority_of(designation: str) -> int:
    """Rank of one designation; 0 if blank or not configured (sorts after all configured ones)."""
    return priority_table().get(" ".join((designation or "").split()).lower(), 0)


def order_by_designation(qs: QuerySet[Any]) -> QuerySet[Any]:
    """Highest designation first, then name A-Z, then employee ID, then id (stable pages)."""
    whens = [
        When(_norm=name, then=Value(rank))
        for name, rank in sorted(priority_table().items(), key=lambda kv: -kv[1])
    ]
    return (
        qs.annotate(_norm=Lower(Trim("designation")))
        .annotate(designation_rank=Case(*whens, default=Value(0), output_field=IntegerField()))
        .order_by("-designation_rank", Lower("full_name"), "employee_id", "id")
    )
