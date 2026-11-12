"""Attendance policies."""

from __future__ import annotations

from typing import Any

from ots.accounts.models import Role
from ots.core.permissions import policy


@policy("session.force_logout")
def _force(user: Any, obj: Any = None) -> bool:
    return bool(user) and user.is_active and user.role == Role.SUPER_ADMIN


@policy("attendance.correct")
def _correct(user: Any, target: Any = None) -> bool:
    """Super Admin for anyone; a Sub-Admin for people in their own team (the caller scopes the person)."""
    return (
        bool(user)
        and user.is_active
        and target is not None
        and user.role in (Role.SUPER_ADMIN, Role.SUB_ADMIN)
    )


@policy("attendance.approve")
def _approve(user: Any, obj: Any = None) -> bool:
    return bool(user) and user.is_active and user.role == Role.SUPER_ADMIN


@policy("holiday.manage")
def _holidays(user: Any, obj: Any = None) -> bool:
    return bool(user) and user.is_active and user.role == Role.SUPER_ADMIN


@policy("selfie.view")
def _selfie(user: Any, session: Any = None) -> bool:
    """Only the Super Admin and the person themselves may see an attendance picture."""
    if not user or not user.is_active or session is None:
        return False
    return user.role == Role.SUPER_ADMIN or session.user_id == user.pk
