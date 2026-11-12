"""Task codes: `T-{YYYY}-{6-digit sequence}`, one PostgreSQL sequence per year (`task_code_seq_2026`).

Migration 0002 creates the sequences for 2026 to 2060, because the app's database role may not run DDL in
production. If a year has no sequence yet, it is created on demand inside the caller's transaction (this
works for the owner role and in development), guarded by an advisory lock so two requests cannot race.
"""

from __future__ import annotations

from django.db import connection, transaction

from ots.core.clock import today_local


def _sequence_name(year: int) -> str:
    return f"task_code_seq_{int(year)}"  # int() makes it impossible to inject anything into the SQL


def next_task_code(year: int | None = None) -> str:
    year = year or today_local().year
    name = _sequence_name(year)
    with connection.cursor() as cur:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", [name])
        exists = cur.fetchone()[0]  # type: ignore[index]
        if not exists:
            with transaction.atomic():
                cur.execute("SELECT pg_advisory_xact_lock(hashtext('ots_task_code_seq'))")
                cur.execute(f"CREATE SEQUENCE IF NOT EXISTS {name}")  # noqa: S608 (name is built from an int)
        cur.execute(f"SELECT nextval('{name}')")  # noqa: S608
        number = cur.fetchone()[0]  # type: ignore[index]
    return f"T-{year}-{number:06d}"
