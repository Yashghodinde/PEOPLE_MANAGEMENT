"""Reads for people and the reporting tree. Recursive CTEs; one query each."""

from __future__ import annotations

import datetime
import uuid

from django.db import connection

_CURRENT = """
WITH RECURSIVE sub(id, depth) AS (
    SELECT id, 1 FROM users WHERE reports_to_id = %s
    UNION ALL
    SELECT u.id, s.depth + 1 FROM users u JOIN sub s ON u.reports_to_id = s.id WHERE s.depth < 50
)
SELECT id FROM sub
"""

_AT = """
WITH RECURSIVE sub(id, depth) AS (
    SELECT h.user_id, 1 FROM reporting_history h
    WHERE h.manager_id = %s AND h.effective_from <= %s AND (h.effective_to IS NULL OR %s < h.effective_to)
    UNION ALL
    SELECT h.user_id, s.depth + 1 FROM reporting_history h JOIN sub s ON h.manager_id = s.id
    WHERE h.effective_from <= %s AND (h.effective_to IS NULL OR %s < h.effective_to) AND s.depth < 50
)
SELECT id FROM sub
"""


def subtree_ids(manager_id: uuid.UUID) -> set[uuid.UUID]:
    """Everyone below `manager_id` today (excludes the manager). One query."""
    with connection.cursor() as cur:
        cur.execute(_CURRENT, [manager_id])
        return {r[0] for r in cur.fetchall()}


def subtree_ids_at(manager_id: uuid.UUID, on: datetime.date) -> set[uuid.UUID]:
    """Everyone below `manager_id` as the tree stood on `on` (from reporting_history). One query.

    A history row covers [effective_from, effective_to): a manager change on day D closes the old row
    with effective_to = D, so the person is still under the old manager on D-1 and under the new one on D.
    """
    with connection.cursor() as cur:
        cur.execute(_AT, [manager_id, on, on, on, on])
        return {r[0] for r in cur.fetchall()}
