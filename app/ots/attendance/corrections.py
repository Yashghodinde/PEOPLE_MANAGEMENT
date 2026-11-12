"""Corrections and holidays (M06 section 3). A Sub-Admin asks for their team, the Super Admin approves."""

from __future__ import annotations

import datetime
from typing import Any

from django.db import transaction

from ots.accounts.models import Role, User
from ots.audit import api as audit
from ots.core import permissions
from ots.core.clock import now, today_local
from ots.core.errors import NotFound, ValidationFailed

from . import rollup
from .models import AttendanceCorrection, AttendanceStatus, Holiday


@transaction.atomic
def request_correction(
    actor: Any, user_id: Any, day: datetime.date, new_status: str, reason: str
) -> AttendanceCorrection:
    target = User.objects.visible_to(actor).filter(pk=user_id, is_break_glass=False).first()
    permissions.require(actor, "attendance.correct", target)
    if target is None:
        raise NotFound("Person not found.")
    if new_status not in AttendanceStatus.values:
        raise ValidationFailed("Choose a status.", fields={"new_status": ["Invalid."]})
    if day > today_local():
        raise ValidationFailed("That day has not happened yet.", fields={"date": ["Future date."]})
    reason = (reason or "").strip()
    if len(reason) < 5:
        raise ValidationFailed("Give a reason.", fields={"reason": ["At least 5 characters."]})
    corr = AttendanceCorrection.objects.create(
        user=target, date=day, new_status=new_status, reason=reason[:1000], requested_by=actor
    )
    audit.record(
        "attendance.correction_requested", actor, target, {"date": str(day), "new_status": new_status}
    )
    if actor.role == Role.SUPER_ADMIN:  # the approver asking: no second step
        _approve(actor, corr)
    return corr


def _approve(actor: Any, corr: AttendanceCorrection) -> None:
    corr.approved_by, corr.approved_at = actor, now()
    corr.save(update_fields=["approved_by", "approved_at"])
    rollup.rollup(corr.date, [corr.user_id])
    audit.record(
        "attendance.correction_approved",
        actor,
        corr.user,
        {"date": str(corr.date), "new_status": corr.new_status},
    )


@transaction.atomic
def approve_correction(actor: Any, correction_id: Any) -> AttendanceCorrection:
    permissions.require(actor, "attendance.approve", None)
    corr = (
        AttendanceCorrection.objects.select_for_update()
        .select_related("user")
        .filter(pk=correction_id)
        .first()
    )
    if corr is None:
        raise NotFound("Correction not found.")
    if corr.approved_at is not None:
        return corr  # approving twice changes nothing
    _approve(actor, corr)
    return corr


# ---- holidays ----


@transaction.atomic
def add_holiday(actor: Any, day: datetime.date, name: str) -> Holiday:
    permissions.require(actor, "holiday.manage", None)
    name = (name or "").strip()
    if not name:
        raise ValidationFailed("Give the holiday a name.", fields={"name": ["Required."]})
    h, created = Holiday.objects.get_or_create(
        company_id=actor.company_id, date=day, defaults={"name": name[:100]}
    )
    if not created and h.name != name[:100]:
        h.name = name[:100]
        h.save(update_fields=["name"])
    audit.record("holiday.saved", actor, None, {"date": str(day), "name": name})
    rollup.rollup(day)
    return h


@transaction.atomic
def remove_holiday(actor: Any, holiday_id: Any) -> None:
    permissions.require(actor, "holiday.manage", None)
    h = Holiday.objects.filter(pk=holiday_id, company_id=actor.company_id).first()
    if h is None:
        raise NotFound("Holiday not found.")
    day = h.date
    h.delete()
    audit.record("holiday.removed", actor, None, {"date": str(day), "name": h.name})
    rollup.rollup(day)
