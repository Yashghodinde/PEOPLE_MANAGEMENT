"""People policies (M02 table, extended by IMP-08). Registered on import; the registry in
ots.core.permissions fails closed.

Who may do what to people is one function, `can_manage_user`:

  create ........ Super Admin: any role. HR: Sub-Admin or Employee. Sub-Admin: an Employee who reports to
                  them. Employee: no.
  update, set manager, deactivate, reset password
                  Super Admin: anyone. HR: Sub-Admins and Employees only (never a Super Admin or another
                  HR account). Sub-Admin and Employee: no.

Nobody but a Super Admin touches a Super Admin account, and nobody creates or edits a role above what the
table gives them. Rank (ROLE_RANK) only decides who may be whose manager; it never grants a right.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ots.core.permissions import policy

from . import selectors
from .models import Role

MANAGE_ACTIONS = ("create", "update", "set_manager", "deactivate", "reset_password")
HR_MAY_MANAGE = (Role.SUB_ADMIN, Role.EMPLOYEE)


@dataclass(frozen=True)
class NewPerson:
    """What `user.create` is asked about: the role being created and the manager it is being created under."""

    role: str
    manager: Any = None


def _is(user: Any, *roles: str) -> bool:
    return bool(user) and getattr(user, "is_active", False) and user.role in roles


def can_manage_user(actor: Any, action: str, target: Any = None) -> bool:
    """The truth table above. `target` is the person acted on (a User), or a NewPerson for `create`.
    With no target the question is only "may this person open the screen for that action"."""
    if action not in MANAGE_ACTIONS or not _is(actor, *Role.values):
        return False
    if actor.role == Role.SUPER_ADMIN:
        return True
    if actor.role == Role.HR:
        if target is None:
            return True
        return str(target.role) in HR_MAY_MANAGE
    if actor.role == Role.SUB_ADMIN:
        if action != "create":
            return False
        if target is None:
            return True
        manager = getattr(target, "manager", None)
        return str(target.role) == Role.EMPLOYEE and manager is not None and manager.pk == actor.pk
    return False


def can_assign_role(actor: Any, role: str) -> bool:
    """May this person put someone into `role` (creating them, or changing their role)?"""
    if not _is(actor, *Role.values):
        return False
    if actor.role == Role.SUPER_ADMIN:
        return role in Role.values
    if actor.role == Role.HR:
        return role in HR_MAY_MANAGE
    if actor.role == Role.SUB_ADMIN:
        return role == Role.EMPLOYEE
    return False


for _a in MANAGE_ACTIONS:
    policy(f"user.{_a}")(lambda user, obj=None, _a=_a: can_manage_user(user, _a, obj))


@policy("user.assign_role")
def _assign_role(user: Any, role: Any = None) -> bool:
    return can_assign_role(user, str(role)) if role is not None else _is(user, Role.SUPER_ADMIN, Role.HR)


@policy("user.change_password")
def _change_password(user: Any, obj: Any = None) -> bool:
    return _is(user, Role.SUPER_ADMIN, Role.SUB_ADMIN, Role.HR, Role.EMPLOYEE) and (
        obj is not None and obj.pk == user.pk
    )


@policy("user.view")
def _view(user: Any, obj: Any = None) -> bool:
    if not _is(user, *Role.values):
        return False
    if user.role in (Role.SUPER_ADMIN, Role.HR) or obj is None:
        return True  # obj None = "may open the people screen"; rows are scoped by User.objects.visible_to
    if obj.pk == user.pk:
        return True
    return user.role == Role.SUB_ADMIN and obj.pk in selectors.subtree_ids(user.pk)


@policy("org.view_chart")
def _chart(user: Any, obj: Any = None) -> bool:
    return _is(user, Role.SUPER_ADMIN, Role.HR, Role.SUB_ADMIN)


@policy("audit.view")
def _audit(user: Any, obj: Any = None) -> bool:
    return _is(user, *Role.values)
