"""All writes for identity and hierarchy: permission check, validate, change + audit + event."""

from __future__ import annotations

from typing import Any

from django.db import IntegrityError, transaction

from ots.audit import api as audit
from ots.core import events, permissions
from ots.core.clock import now, today_local
from ots.core.errors import Conflict, Locked, ValidationFailed

from . import auth, passwords, photos, selectors
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
    if manager.is_break_glass:
        raise ValidationFailed(
            "The break-glass account cannot manage people.", fields={"reports_to": ["Not allowed."]}
        )
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
    """Close the current reporting row and open a new one. A change on the same day as the current row
    started rewrites that row instead, so history never holds an empty [D, D) interval."""
    today = today_local()
    current = ReportingHistory.objects.filter(user=user, effective_to__isnull=True).first()
    if current is not None and current.effective_from >= today:
        current.manager = manager
        current.changed_by = actor
        current.save(update_fields=["manager", "changed_by"])
        return
    ReportingHistory.objects.filter(user=user, effective_to__isnull=True).update(effective_to=today)
    ReportingHistory.objects.create(user=user, manager=manager, effective_from=today, changed_by=actor)


def _lock_pair(user: User, manager: User | None, expected_version: int | None) -> tuple[User, User | None]:
    """Lock the person and the manager in ONE query ordered by id, so two admins swapping the same two
    people cannot deadlock, and nobody can deactivate the manager while we attach someone to them."""
    ids = [user.pk] + ([manager.pk] if manager is not None else [])
    rows = {u.pk: u for u in User.objects.select_for_update().filter(pk__in=ids).order_by("pk")}
    cur = rows[user.pk]
    if expected_version is not None and cur.version != expected_version:
        raise Conflict("This person was changed by someone else. Reload and try again.", current=cur.version)
    return cur, (rows[manager.pk] if manager is not None else None)


def _refuse_break_glass(user: User) -> None:
    if user.is_break_glass:
        raise ValidationFailed("The break-glass account can only be changed from the server shell.")


def _db_refused(exc: IntegrityError) -> ValidationFailed:
    """Turn a database constraint or trigger refusal into a friendly validation error."""
    text = str(exc)
    if "employee_id" in text:
        return ValidationFailed("That employee ID is already used.", fields={"employee_id": ["Taken."]})
    if "login_id" in text:
        return ValidationFailed("That login ID is already used.", fields={"login_id": ["Taken."]})
    return ValidationFailed("The database refused this change because it breaks a hierarchy rule.")


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
    if reports_to is not None:  # lock the manager so they cannot be deactivated while we attach someone
        reports_to = User.objects.select_for_update().get(pk=reports_to.pk)
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
    try:
        with transaction.atomic():
            user.save()
    except IntegrityError as exc:  # two admins created the same ID at once
        raise _db_refused(exc) from exc
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


KEEP: Any = object()  # update_profile(reports_to=KEEP): leave the manager as it is


def _manager_changed_records(
    actor: User | None, cur: User, old_id: Any, manager: User | None, request: Any
) -> None:
    _open_history(cur, manager, actor)
    audit.record(
        "user.manager_changed",
        actor,
        cur,
        {"from": str(old_id) if old_id else None, "to": str(manager.pk) if manager else None},
        request,
    )
    events.publish("user.manager_changed", cur, {"manager_id": str(manager.pk) if manager else None})


@transaction.atomic
def update_profile(
    actor: User,
    user: User,
    *,
    expected_version: int | None = None,
    reports_to: Any = KEEP,
    request: Any = None,
    **changes: Any,
) -> User:
    """Change profile fields, and optionally the role and manager in the same save.

    Passing `reports_to` together with `role` is how a Super Admin is demoted (needs a manager at the
    same moment) and how a promotion gets its higher-ranked manager. Role and manager are validated
    against each other, then written in one UPDATE so the database triggers see the final state.
    """
    permissions.require(actor, "user.update", user)
    unknown = set(changes) - PROFILE_FIELDS
    if unknown:
        raise ValidationFailed(f"Unknown fields: {', '.join(sorted(unknown))}")
    # lock person + the manager we are validating against (new one if given, else the current one)
    candidate = user.reports_to if reports_to is KEEP else reports_to
    cur, locked_manager = _lock_pair(user, candidate, expected_version)
    _refuse_break_glass(cur)
    if reports_to is KEEP:
        if locked_manager is not None and cur.reports_to_id != locked_manager.pk:
            raise Conflict("This person was changed by someone else. Reload and try again.")
        manager = locked_manager
    else:
        manager = locked_manager
    if "employee_id" in changes and changes["employee_id"] != cur.employee_id:
        if User.objects.filter(employee_id=changes["employee_id"]).exclude(pk=cur.pk).exists():
            raise ValidationFailed("That employee ID is already used.", fields={"employee_id": ["Taken."]})
    new_role = changes.get("role", cur.role)
    role_changed = new_role != cur.role
    manager_changed = reports_to is not KEEP and (manager.pk if manager else None) != cur.reports_to_id
    if role_changed:
        if new_role not in Role.values:
            raise ValidationFailed("Unknown role.", fields={"role": ["Invalid."]})
        if new_role == Role.SUPER_ADMIN:
            raise ValidationFailed("Promote to Super Admin by creating a new account.")
        if _rank(new_role) < _rank(cur.role):
            _last_super_admin_guard(cur)
            if [r for r in _active_reports(cur) if _rank(r.role) >= _rank(new_role)]:
                _require_no_reports(cur, "change the role")
    if role_changed or manager_changed:
        _check_manager(new_role, manager, cur.pk)
    changed = {}
    for k, v in changes.items():
        if getattr(cur, k) != v:
            changed[k] = [str(getattr(cur, k)), str(v)]
            setattr(cur, k, v)
    if not changed and not manager_changed:
        return cur
    old_manager_id = cur.reports_to_id
    if manager_changed:
        cur.reports_to = manager
    cur.version += 1
    try:
        with transaction.atomic():
            cur.save()
    except IntegrityError as exc:
        raise _db_refused(exc) from exc
    if manager_changed:
        _manager_changed_records(actor, cur, old_manager_id, manager, request)
    if changed:
        audit.record("user.updated", actor, cur, {"changed": changed}, request)
        events.publish("user.updated", cur, {"fields": sorted(changed)})
    return cur


@transaction.atomic
def set_manager(
    actor: User, user: User, manager: User | None, *, expected_version: int | None = None, request: Any = None
) -> User:
    permissions.require(actor, "user.set_manager", user)
    cur, manager = _lock_pair(user, manager, expected_version)
    _refuse_break_glass(cur)
    _check_manager(cur.role, manager, cur.pk)
    old = cur.reports_to_id
    if (manager.pk if manager else None) == old:
        return cur
    cur.reports_to = manager
    cur.version += 1
    try:
        with transaction.atomic():
            cur.save()
    except IntegrityError as exc:
        raise _db_refused(exc) from exc
    _manager_changed_records(actor, cur, old, manager, request)
    return cur


@transaction.atomic
def deactivate_user(
    actor: User, user: User, *, expected_version: int | None = None, request: Any = None
) -> User:
    permissions.require(actor, "user.deactivate", user)
    cur = _locked(user, expected_version)
    _refuse_break_glass(cur)
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
def reactivate_user(
    actor: User, user: User, *, expected_version: int | None = None, request: Any = None
) -> User:
    permissions.require(actor, "user.update", user)
    cur, manager = _lock_pair(
        user, user.reports_to, expected_version
    )  # manager locked: cannot be deactivated now
    _refuse_break_glass(cur)
    if cur.is_active:
        return cur
    if manager is not None and (cur.reports_to_id != manager.pk or not manager.is_active):
        raise ValidationFailed("Their manager is inactive. Assign an active manager first.")
    cur.is_active = True
    cur.version += 1
    try:
        with transaction.atomic():
            cur.save()
    except IntegrityError as exc:
        raise _db_refused(exc) from exc
    audit.record("user.reactivated", actor, cur, {}, request)
    events.publish("user.updated", cur, {"fields": ["is_active"]})
    return cur


@transaction.atomic
def reset_password(
    actor: User, user: User, new_password: str, *, expected_version: int | None = None, request: Any = None
) -> User:
    permissions.require(actor, "user.reset_password", user)
    passwords.validate(new_password, user.login_id)
    cur = _locked(user, expected_version)
    cur.set_password(new_password)
    cur.must_change_password = True
    cur.failed_logins = 0
    cur.locked_until = None
    cur.version += 1
    cur.save()
    audit.record("user.password_reset", actor, cur, {}, request)
    events.publish("user.updated", cur, {"fields": ["password"]})
    return cur


def change_password(user: User, old_password: str, new_password: str, *, request: Any = None) -> User:
    """Change your own password. A wrong current password counts toward the same lockout as /login.

    Not wrapped in one outer transaction on purpose: the failure row and the lock must be committed
    even though the call ends in an error, so the error is raised after the block has committed.
    """
    permissions.require(user, "user.change_password", user)
    t = now()
    failure: Locked | ValidationFailed
    with transaction.atomic():
        cur = _locked(user, None)
        if cur.locked_until and cur.locked_until > t:
            audit.record(
                "auth.login_blocked", cur, cur, {"reason": "locked", "via": "password_change"}, request
            )
            minutes = auth.minutes_left(cur.locked_until, t)
            failure = Locked(f"Too many wrong attempts. Try again in {minutes} minute(s).", minutes=minutes)
        elif not cur.check_password(old_password):
            minutes = auth.record_failure(cur, request, "auth.password_change_failed", "bad_old_password", t)
            if minutes:
                failure = Locked(
                    f"Too many wrong attempts. Try again in {minutes} minute(s).", minutes=minutes
                )
            else:
                failure = ValidationFailed(
                    "The current password is wrong.", fields={"old_password": ["Wrong."]}
                )
        else:
            if old_password == new_password:
                raise ValidationFailed(
                    "Choose a different password.", fields={"password": ["Same as before."]}
                )
            passwords.validate(new_password, cur.login_id)
            cur.set_password(new_password)
            cur.must_change_password = False
            cur.failed_logins = 0
            cur.locked_until = None
            cur.version += 1
            cur.save()
            audit.record("user.password_changed", cur, cur, {}, request)
            events.publish("user.updated", cur, {"fields": ["password"]})
            return cur
    raise failure


@transaction.atomic
def set_reference_photo(
    actor: User, user: User, data: bytes, *, expected_version: int | None = None, request: Any = None
) -> User:
    """Store a reference photo (a cropped 480 x 480 JPEG). The file is written only after commit."""
    permissions.require(actor, "user.update", user)
    jpeg = photos.process(data)  # raises ValidationFailed before anything is touched
    cur = _locked(user, expected_version)
    _refuse_break_glass(cur)
    cur.reference_photo_path = f"reference/{cur.pk}.jpg"
    cur.version += 1
    cur.save()
    audit.record("user.photo_updated", actor, cur, {"bytes": len(jpeg)}, request)
    events.publish("user.updated", cur, {"fields": ["reference_photo"]})
    user_id = cur.pk
    transaction.on_commit(lambda: photos.write(user_id, jpeg))
    return cur


@transaction.atomic
def remove_reference_photo(
    actor: User, user: User, *, expected_version: int | None = None, request: Any = None
) -> User:
    permissions.require(actor, "user.update", user)
    cur = _locked(user, expected_version)
    _refuse_break_glass(cur)
    if not cur.reference_photo_path:
        return cur
    cur.reference_photo_path = ""
    cur.version += 1
    cur.save()
    audit.record("user.photo_removed", actor, cur, {}, request)
    events.publish("user.updated", cur, {"fields": ["reference_photo"]})
    user_id = cur.pk
    transaction.on_commit(lambda: photos.delete(user_id))
    return cur
