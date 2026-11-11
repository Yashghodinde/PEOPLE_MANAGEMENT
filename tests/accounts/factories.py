"""Test helpers: build users with unique codes. Bypasses services on purpose (services come in M02.2)."""

import datetime
import itertools

from ots.accounts.models import Company, ReportingHistory, Role, User

_n = itertools.count(1)


def company() -> Company:
    return Company.objects.first() or Company.objects.create(name="Test Co", code="TEST")


def make_user(role: str = Role.EMPLOYEE, reports_to: User | None = None, **kw) -> User:  # type: ignore[no-untyped-def]
    i = next(_n)
    if reports_to is None and role != Role.SUPER_ADMIN:
        raise ValueError("reports_to required")
    kw.setdefault("login_id", f"user{i}")
    kw.setdefault("employee_id", f"EMP{i:04d}")
    kw.setdefault("full_name", f"User {i}")
    u = User(company=company(), role=role, reports_to=reports_to, **kw)
    u.set_password("correct horse battery")
    u.save()
    ReportingHistory.objects.create(user=u, manager=reports_to, effective_from=datetime.date(2000, 1, 1))
    return u
