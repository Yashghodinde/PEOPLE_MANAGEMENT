"""Help requests (IMP-15.11): an employee says what they wait for; the manager answers in the same thread.

The request has its own status. The assignment goes BLOCKED through the ordinary `block` action, so the task
lifecycle is unchanged and resolving a request never completes anything: the employee resumes with "Back to
work". Every message is an ordinary task comment linked to the request, so there is one chat, not two.
"""

from __future__ import annotations

from typing import Any

from django.db import transaction
from django.http import HttpRequest

from ots.accounts import selectors as accounts
from ots.accounts.models import Role
from ots.audit import api as audit
from ots.core.clock import now
from ots.core.errors import NotFound, PermissionDenied, ValidationFailed
from ots.notifications.models import Kind, Notification

from . import services
from .models import AssignmentStatus, BlockReason, SupportKind, SupportRequest, SupportStatus, TaskAssignment

OPEN_STATES = (SupportStatus.OPEN, SupportStatus.MANAGER_RESPONDED, SupportStatus.WAITING_FOR_EMPLOYEE)
BLOCK_OF = {k: BlockReason.INSUFFICIENT_INPUT for k in SupportKind.values}
BLOCK_OF[SupportKind.EXTERNAL_DEPENDENCY] = BlockReason.DEPENDENCY


def _line(sr: SupportRequest) -> str:
    out = [f"{sr.get_kind_display()}{' (urgent)' if sr.urgent else ''}", sr.what.strip()]
    if sr.why.strip():
        out.append(f"Why it is needed: {sr.why.strip()}")
    if sr.impact.strip():
        out.append(f"What cannot continue: {sr.impact.strip()}")
    return "\n".join(out)


@transaction.atomic
def raise_request(
    actor: Any, assignment_id: Any, *, kind: str, what: str, why: str = "", impact: str = "",
    urgent: bool = False, request: HttpRequest | None = None,
) -> SupportRequest:  # fmt: skip
    if kind not in SupportKind.values:
        raise ValidationFailed("Say what kind of help you need.", fields={"kind": ["Invalid."]})
    what = " ".join((what or "").split())
    if len(what) < 5:
        raise ValidationFailed("Explain what you need.", fields={"what": ["Required."]})
    a = TaskAssignment.objects.select_related("task", "assigned_by").filter(pk=assignment_id).first()
    if a is None or a.user_id != actor.pk:
        raise NotFound("Not found.")  # only the person on the assignment raises it
    t0 = now()
    services.transition(
        a.pk, "block", actor, block_reason=BLOCK_OF[kind], block_note=what
    )  # permission checked here
    a.refresh_from_db()
    manager = a.assigned_by if a.assigned_by.is_active else a.task.created_by
    sr = SupportRequest.objects.create(
        task=a.task, assignment=a, raised_by=actor, manager=manager, kind=kind, what=what,
        why=" ".join((why or "").split())[:1000], impact=" ".join((impact or "").split())[:1000],
        urgent=bool(urgent), created_at=t0,
    )  # fmt: skip
    comment = services.add_comment(actor, a.task_id, _line(sr), a.pk, help_request=sr)
    link = f"/app/tasks/{a.task_id}?request={sr.pk}"
    Notification.objects.filter(task=a.task, created_at__gte=t0, link="").filter(kind=Kind.BLOCKED).update(
        link=link
    )  # the "someone is waiting" bell opens this very request
    Notification.objects.filter(comment=comment, link="").update(link=link)
    audit.record("support.raised", actor, sr, {"kind": kind, "task": str(a.task_id)}, request)
    return sr


def may_answer(actor: Any, sr: SupportRequest) -> bool:
    if actor.role == Role.SUPER_ADMIN:
        return True
    if actor.pk in (sr.manager_id, sr.task.created_by_id, sr.assignment.assigned_by_id):
        return True
    return actor.role == Role.SUB_ADMIN and sr.raised_by_id in accounts.subtree_ids(actor.pk)


@transaction.atomic
def reply(
    actor: Any, request_id: Any, body: str, action: str = "reply", request: HttpRequest | None = None
) -> SupportRequest:
    """Manager side: `reply`, `clarify` or `resolve`. The employee's `reply` goes back to the manager."""
    sr = (
        SupportRequest.objects.select_for_update(of=("self",))
        .select_related("task", "assignment", "raised_by")
        .filter(pk=request_id)
        .first()
    )
    employee = sr is not None and actor.pk == sr.raised_by_id
    if sr is None or not (employee or may_answer(actor, sr)):
        raise NotFound("Not found.")
    if sr.status not in OPEN_STATES:
        raise ValidationFailed("This request is already resolved.")
    if action not in ("reply", "clarify", "resolve"):
        raise ValidationFailed("Choose what to do.")
    if employee and action != "reply":
        raise PermissionDenied("Only the manager can do that.", action="support.answer")
    text = " ".join((body or "").split())
    if not text and action != "resolve":
        raise ValidationFailed("Write your reply.", fields={"body": ["Required."]})
    if text:
        services.add_comment(actor, sr.task_id, text, sr.assignment_id, help_request=sr)
    if employee:
        sr.status = SupportStatus.OPEN
    elif action == "reply":
        sr.status = SupportStatus.MANAGER_RESPONDED
    elif action == "clarify":
        sr.status = SupportStatus.WAITING_FOR_EMPLOYEE
    else:
        sr.status, sr.resolved_at, sr.resolved_by = SupportStatus.RESOLVED, now(), actor
    sr.save(update_fields=["status", "resolved_at", "resolved_by"])
    audit.record(f"support.{action}", actor, sr, {"status": sr.status}, request)
    return sr


def answered_assignment_ids(assignment_ids: list[Any]) -> set[Any]:
    """Assignments with a waiting request the manager has answered: the card turns pink (D-100)."""
    return set(
        SupportRequest.objects.filter(
            assignment_id__in=assignment_ids,
            status__in=(SupportStatus.MANAGER_RESPONDED, SupportStatus.WAITING_FOR_EMPLOYEE),
            assignment__status=AssignmentStatus.BLOCKED,
        ).values_list("assignment_id", flat=True)
    )
