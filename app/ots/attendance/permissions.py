"""Attendance policies."""

from __future__ import annotations

from typing import Any

from ots.accounts.models import Role
from ots.core.permissions import policy


@policy("session.force_logout")
def _force(user: Any, obj: Any = None) -> bool:
    return bool(user) and user.is_active and user.role == Role.SUPER_ADMIN
