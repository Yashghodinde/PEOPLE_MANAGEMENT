"""People policies (M02 table). Registered at import; fail-closed registry in ots.core.permissions."""

from __future__ import annotations

from typing import Any

from ots.core.permissions import policy

from . import selectors
from .models import Role


def _is(user: Any, *roles: str) -> bool:
    return bool(user) and getattr(user, "is_active", False) and user.role in roles


def _admin_only(user: Any, obj: Any = None) -> bool:
    return _is(user, Role.SUPER_ADMIN)


for _a in ("user.create", "user.update", "user.deactivate", "user.set_manager", "user.reset_password"):
    policy(_a)(_admin_only)


@policy("user.change_password")
def _change_password(user: Any, obj: Any = None) -> bool:
    return (
        _is(user, Role.SUPER_ADMIN, Role.SUB_ADMIN, Role.EMPLOYEE) and obj is not None and obj.pk == user.pk
    )


@policy("user.view")
def _view(user: Any, obj: Any = None) -> bool:
    if not _is(user, *Role.values):
        return False
    if user.role == Role.SUPER_ADMIN or obj is None:
        return True  # obj None = "may open the people screen"; rows are scoped by User.objects.visible_to
    if obj.pk == user.pk:
        return True
    return user.role == Role.SUB_ADMIN and obj.pk in selectors.subtree_ids(user.pk)


@policy("org.view_chart")
def _chart(user: Any, obj: Any = None) -> bool:
    return _is(user, Role.SUPER_ADMIN, Role.SUB_ADMIN)


@policy("audit.view")
def _audit(user: Any, obj: Any = None) -> bool:
    return _is(user, *Role.values)
