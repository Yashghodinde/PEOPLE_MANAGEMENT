"""All writes for identity and hierarchy: permission check, validate, change + audit + event."""

from __future__ import annotations

from typing import Any

from django.db import transaction

from ots.audit import api as audit
from ots.core import events, permissions
from ots.core.clock import now, today_local
from ots.core.errors import Conflict, ValidationFailed

from . import passwords, selectors
from .models import ROLE_RANK, Company, Department, ReportingHistory, Role, User

PROFILE_FIELDS = {
    "full_name",
    "designation",
    "department",
    "date_of_joining",
    "highest_qualification",
    "qualification_detail",
    "institution",
    "qualification_year",
    "shift_start",
    "shift_end",
    "weekly_off",
    "mobile",
    "email",
    "employee_id",
    "role",
}


def _rank(role: str) -> int:
    return ROLE_RANK[Role(role)]


def _check_manager(role: str, manager: User | None, user_id: Any = None) -> None:
    """Friendly validation; the database triggers enforce the same rules as a backstop."""
    if role == Role.SUPER_ADMIN:
        if manager is not None:
            raise ValidationFailed("A Super Admin has no manager.", fields={"reports_to": ["Not allowed."]})
        return
    if manager is None:
        raise ValidationFailed("Choose who this person reports to.", fields={"reports_to": ["Required."]})
    if not manager.is_active:
        raise ValidationFailed("The chosen manager is not active.", fields={"reports_to": ["Inactive."]})
    if _rank(manager.role) <= _rank(role):
        raise ValidationFailed(
            "The manager must have a higher role than the person.", fields={"reports_to": ["Rank too low."]}
        )
    if user_id is not None and (manager.pk == user_id or manager.pk in selectors.subtree_ids(user_id)):
        raise ValidationFailed(
            "That would create a reporting loop.", fields={"reports_to": ["Creates a loop."]}
        )


def _locked(user: User, expected_version: int | None) -> User:
    cur = User.objects.select_for_update().get(pk=user.pk)
    if expected_version is not None and cur.version != expected_version:
        raise Conflict("This person was changed by someone else. Reload and try again.", current=cur.version)
    return cur


def _open_history(user: User, manager: User | None, actor: User | None) -> None:
    today = today_local()
    ReportingHistory.objects.filter(user=user, effective_to__isnull=True).update(effective_to=today)
    ReportingHistory.objects.create(user=user, manager=manager, effective_from=today, changed_by=actor)


def _active_reports(user: User) -> list[User]:
    return list(User.objects.filter(reports_to=user, is_active=True).order_by("full_name"))


def _require_no_reports(user: User, what: str) -> None:
    reports = _active_reports(user)
    if reports:
        names = ", ".join(r.full_name for r in reports)
        raise ValidationFailed(
            f"{user.full_name} still has people reporting to them ({names}). Move them first, then {what}.",
            fields={"reports": [r.full_name for r in reports]},
        )


def _last_super_admin_guard(user: User) -> None:
    if user.role == Role.SUPER_ADMIN and user.is_active and not user.is_break_glass:
        others = User.objects.filter(role=Role.SUPER_ADMIN, is_active=True, is_break_glass=False).exclude(
            pk=user.pk
        )
        if not others.exists():
            raise ValidationFailed("This is the last active Super Admin. Create another first.")


@transaction.atomic
def create_user(
    actor: User | None,
    *,
    login_id: str,
    employee_id: str,
    full_name: str,
    role: str,
    reports_to: User | None,
    password: str,
    company: Company | None = None,
    must_change_password: bool = True,
    is_break_glass: bool = False,
    request: Any = None,
    **profile: Any,
) -> User:
    if actor is not None:  # actor None only for bootstrap_admins (server shell)
        permissions.require(actor, "user.create")
    unknown = set(profile) - PROFILE_FIELDS
    if unknown:
        raise ValidationFailed(f"Unknown fields: {', '.join(sorted(unknown))}")
    if role not in Role.values:
        raise ValidationFailed("Unknown role.", fields={"role": ["Invalid."]})
    if User.objects.filter(login_id__iexact=login_id).exists():
        raise ValidationFailed("That login ID is already used.", fields={"login_id": ["Taken."]})
    if User.objects.filter(employee_id=employee_id).exists():
        raise ValidationFailed("That employee ID is already used.", fields={"employee_id": ["Taken."]})
    _check_manager(role, reports_to)
    passwords.validate(password, login_id)
    dept = profile.get("department")
    if dept is not None and not isinstance(dept, Department):
        raise ValidationFailed("Unknown department.", fields={"department": ["Invalid."]})
    user = User(
        company=company or Company.objects.get(code="DEFAULT"),
        login_id=login_id,
        employee_id=employee_id,
        full_name=full_name,
        role=role,
        reports_to=reports_to,
        must_change_password=must_change_password,
        is_break_glass=is_break_glass,
        **profile,
    )
    user.set_password(password)
    user.save()
    _open_history(user, reports_to, actor)
    audit.record(
        "user.created",
        actor,
        user,
        {"login_id": login_id, "role": role, "reports_to": str(reports_to.pk) if reports_to else None},
        request,
    )
    events.publish("user.created", user, {"role": role})
    return user


@transaction.atomic
def update_profile(
    actor: User, user: User, *, expected_version: int | None = None, request: Any = None, **changes: Any
) -> User:
    permissions.require(actor, "user.update", user)
    unknown = set(changes) - PROFILE_FIELDS
    if unknown:
        raise ValidationFailed(f"Unknown fields: {', '.join(sorted(unknown))}")
    cur = _locked(user, expected_version)
    if "employee_id" in changes and changes["employee_id"] != cur.employee_id:
        if User.objects.filter(employee_id=changes["employee_id"]).exclude(pk=cur.pk).exists():
            raise ValidationFailed("That employee ID is already used.", fields={"employee_id": ["Taken."]})
    new_role = changes.get("role", cur.role)
    if new_role != cur.role:
        if new_role not in Role.values:
            raise ValidationFailed("Unknown role.", fields={"role": ["Invalid."]})
        if _rank(new_role) < _rank(cur.role):
            _last_super_admin_guard(cur)
            reports = [r for r in _active_reports(cur) if _rank(r.role) >= _rank(new_role)]
            if reports:
                _require_no_reports(cur, "change the role")
        manager = cur.reports_to
        if new_role == Role.SUPER_ADMIN:
            raise ValidationFailed("Promote to Super Admin by creating a new account.")
        _check_manager(new_role, manager, cur.pk)
    changed = {}
    for k, v in changes.items():
        if getattr(cur, k) != v:
            changed[k] = [str(getattr(cur, k)), str(v)]
            setattr(cur, k, v)
    if not changed:
        return cur
    cur.version += 1
    cur.save()
    audit.record("user.updated", actor, cur, {"changed": changed}, request)
    events.publish("user.updated", cur, {"fields": sorted(changed)})
    return cur


@transaction.atomic
def set_manager(
    actor: User, user: User, manager: User | None, *, expected_version: int | None = None, request: Any = None
) -> User:
    permissions.require(actor, "user.set_manager", user)
    cur = _locked(user, expected_version)
    if manager is not None:
        manager = User.objects.select_for_update().get(pk=manager.pk)
    _check_manager(cur.role, manager, cur.pk)
    old = cur.reports_to_id
    if (manager.pk if manager else None) == old:
        return cur
    cur.reports_to = manager
    cur.version += 1
    cur.save()
    _open_history(cur, manager, actor)
    audit.record(
        "user.manager_changed",
        actor,
        cur,
        {"from": str(old) if old else None, "to": str(manager.pk) if manager else None},
        request,
    )
    events.publish("user.manager_changed", cur, {"manager_id": str(manager.pk) if manager else None})
    return cur


@transaction.atomic
def deactivate_user(
    actor: User, user: User, *, expected_version: int | None = None, request: Any = None
) -> User:
    permissions.require(actor, "user.deactivate", user)
    cur = _locked(user, expected_version)
    if not cur.is_active:
        return cur
    if cur.pk == actor.pk:
        raise ValidationFailed("You cannot deactivate your own account.")
    _require_no_reports(cur, "deactivate")
    _last_super_admin_guard(cur)
    cur.is_active = False
    cur.version += 1
    cur.save()
    audit.record("user.deactivated", actor, cur, {}, request)
    events.publish("user.deactivated", cur, {})
    return cur


@transaction.atomic
def reactivate_user(actor: User, user: User, *, request: Any = None) -> User:
    permissions.require(actor, "user.update", user)
    cur = _locked(user, None)
    if cur.is_active:
        return cur
    if cur.reports_to_id and not User.objects.filter(pk=cur.reports_to_id, is_active=True).exists():
        raise ValidationFailed("Their manager is inactive. Assign an active manager first.")
    cur.is_active = True
    cur.version += 1
    cur.save()
    audit.record("user.reactivated", actor, cur, {}, request)
    events.publish("user.updated", cur, {"fields": ["is_active"]})
    return cur


@transaction.atomic
def reset_password(actor: User, user: User, new_password: str, *, request: Any = None) -> User:
    permissions.require(actor, "user.reset_password", user)
    passwords.validate(new_password, user.login_id)
    cur = _locked(user, None)
    cur.set_password(new_password)
    cur.must_change_password = True
    cur.failed_logins = 0
    cur.locked_until = None
    cur.version += 1
    cur.save()
    audit.record("user.password_reset", actor, cur, {}, request)
    events.publish("user.updated", cur, {"fields": ["password"]})
    return cur


@transaction.atomic
def change_password(user: User, old_password: str, new_password: str, *, request: Any = None) -> User:
    permissions.require(user, "user.change_password", user)
    cur = _locked(user, None)
    if not cur.check_password(old_password):
        raise ValidationFailed("The current password is wrong.", fields={"old_password": ["Wrong."]})
    if old_password == new_password:
        raise ValidationFailed("Choose a different password.", fields={"password": ["Same as before."]})
    passwords.validate(new_password, cur.login_id)
    cur.set_password(new_password)
    cur.must_change_password = False
    cur.version += 1
    cur.save()
    audit.record("user.password_changed", cur, cur, {}, request)
    events.publish("user.updated", cur, {"fields": ["password"]})
    return cur


def record_login_time(user: User) -> None:  # used by M02.3
    User.objects.filter(pk=user.pk).update(last_login_at=now())
