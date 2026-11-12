"""Task screens (M07.5): list, detail, create form, the action forms, comments and the dashboards.

Views stay thin: services do every write. Forms are plain HTML posts (they work without JavaScript), so the
POST views accept a missing Idempotency-Key; a key that is sent is still enforced."""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Prefetch, Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.utils.html import format_html
from django.views.decorators.http import require_http_methods

from ots.accounts.models import Role
from ots.core import permissions
from ots.core.clock import IST, now
from ots.core.errors import NotFound, OtsError, ValidationFailed
from ots.core.idempotency import idempotency_optional

from . import dashboard, selectors, services, state
from .models import (
    OPEN_STATUSES,
    AssignmentStatus,
    BlockReason,
    DerivedStatus,
    Priority,
    Task,
    TaskAssignment,
)

PAGE_SIZE = 50


def _done(request: HttpRequest, url: str) -> HttpResponse:
    """HTMX submits get an HX-Redirect; plain submits a normal redirect."""
    if request.headers.get("HX-Request") == "true":
        resp = HttpResponse(status=200)
        resp["HX-Redirect"] = url
        return resp
    return redirect(url)


def local_datetime(raw: str, field: str, *, required: bool) -> datetime.datetime | None:
    """A `datetime-local` form value ("2026-11-20T17:30") read as Asia/Kolkata time."""
    raw = (raw or "").strip()
    if not raw:
        if required:
            raise ValidationFailed("Choose a date and time.", fields={field: ["Required."]})
        return None
    try:
        return datetime.datetime.fromisoformat(raw).replace(tzinfo=IST)
    except ValueError:
        raise ValidationFailed("That date and time is not valid.", fields={field: ["Invalid."]}) from None


def uuid_list(values: list[str], field: str) -> list[uuid.UUID]:
    try:
        return [uuid.UUID(v) for v in values if v]
    except ValueError:
        raise ValidationFailed("That person is not valid.", fields={field: ["Invalid."]}) from None


def _uuid(raw: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(raw).strip())
    except (ValueError, AttributeError, TypeError):
        return None


def _version(raw: Any) -> int | None:
    """The version the form was drawn with. Missing or not a number is a stale form, never a 500."""
    try:
        return int(str(raw).strip())
    except (ValueError, TypeError):
        raise ValidationFailed("This page is out of date. Reload it and try again.") from None


def _task_or_404(request: HttpRequest, raw: Any) -> Task:
    pk = _uuid(raw)
    task = Task.objects.visible_to(request.user).select_related("created_by").filter(pk=pk).first() if pk else None
    if task is None:
        raise NotFound("Task not found.")
    return task


def _status_text(viewer: Any, a: TaskAssignment) -> str:
    """Employees see the friendly wording; managers see the real status."""
    if viewer.role == Role.EMPLOYEE:
        return state.employee_label(a.status, a.block_reason)
    return str(a.status).replace("_", " ").capitalize()


# ---------------------------------------------------------------- list


def _live_assignments() -> Prefetch:
    return Prefetch(
        "assignments",
        queryset=TaskAssignment.objects.filter(removed_at__isnull=True).select_related("user"),
        to_attr="live",
    )


def _page_url(params: Any, number: int) -> str:
    keep = [(k, v) for k, v in params.items() if k != "page" and v != ""]
    return "?" + urlencode([*keep, ("page", number)])


@login_required
@require_http_methods(["GET"])
def task_list(request: HttpRequest) -> HttpResponse:
    permissions.require(request.user, "task.view")
    g = request.GET
    qs = Task.objects.visible_to(request.user).select_related("created_by").prefetch_related(_live_assignments())
    if g.get("status") in DerivedStatus.values:
        qs = qs.filter(derived_status=g["status"])
    if g.get("priority") in Priority.values:
        qs = qs.filter(priority=g["priority"])
    if g.get("overdue") == "1":
        qs = qs.overdue(now())
    if g.get("mine") == "1":
        qs = qs.filter(assignments__user=request.user, assignments__removed_at__isnull=True)
    q = (g.get("q") or "").replace("\x00", "").strip()[:100]
    if q:
        qs = qs.filter(Q(title__icontains=q) | Q(task_code__icontains=q))
    page = Paginator(qs.order_by("due_at", "-created_at"), PAGE_SIZE).get_page(g.get("page"))
    at = now()
    rows = []
    for t in page:
        names = [a.user.full_name for a in t.live]
        who = ", ".join(names[:3]) + (f" +{len(names) - 3}" if len(names) > 3 else "")
        late = t.due_at < at and t.derived_status not in (DerivedStatus.COMPLETED, DerivedStatus.CANCELLED)
        rows.append([
            format_html('<a class="text-accent hover:text-ink-900" href="/app/tasks/{}">{}</a>', t.pk, t.task_code),
            t.title,
            t.get_priority_display(),
            who,
            format_html('<span class="{}">{}</span>', "text-bad" if late else "", t.due_at.astimezone(IST).strftime("%d %b, %H:%M")),
            t.get_derived_status_display() + (" · waiting" if t.has_blocked else ""),
        ])  # fmt: skip
    ctx = {
        "page_title": "Tasks",
        "columns": [{"label": c} for c in ("Code", "Title", "Priority", "People", "Due", "Status")],
        "rows": rows,
        "page": page,
        "f": {k: g.get(k, "") for k in ("q", "status", "priority", "overdue", "mine")},
        "statuses": DerivedStatus.choices,
        "priorities": Priority.choices,
        "can_create": permissions.can(request.user, "task.create"),
        "prev_url": _page_url(g, page.previous_page_number()) if page.has_previous() else "",
        "next_url": _page_url(g, page.next_page_number()) if page.has_next() else "",
    }
    return render(request, "tasks/task_list.html", ctx)


# ---------------------------------------------------------------- create


def _create_ctx(request: HttpRequest, values: dict[str, Any], errors: dict[str, Any], message: str = "") -> dict[str, Any]:
    return {
        "page_title": "New task",
        "v": values,
        "errors": errors,
        "message": message,
        "people": selectors.assignable_users(request.user),
        "priorities": Priority.choices,
        "chosen": set(values.get("assignees", [])),
    }


@login_required
@require_http_methods(["GET"])
def task_new(request: HttpRequest) -> HttpResponse:
    permissions.require(request.user, "task.create")
    return render(request, "tasks/task_form.html", _create_ctx(request, {"priority": "NORMAL"}, {}))


@login_required
@idempotency_optional
@require_http_methods(["POST"])
def task_create(request: HttpRequest) -> HttpResponse:
    p: Any = request.POST
    try:
        start = local_datetime(p.get("scheduled_start_at", ""), "scheduled_start_at", required=False)
        task = services.create_task(
            request.user,
            title=p.get("title", ""),
            description=p.get("description", ""),
            priority=p.get("priority", "NORMAL"),
            category=p.get("category", ""),
            due_at=local_datetime(p.get("due_at", ""), "due_at", required=True),  # type: ignore[arg-type]
            scheduled_start_at=start,
            assignee_ids=uuid_list(p.getlist("assignees"), "assignees"),
            hide_from_display=bool(p.get("hide_from_display")),
        )
    except ValidationFailed as exc:
        values = {k: p.get(k, "") for k in ("title", "description", "priority", "category", "due_at", "scheduled_start_at")}
        values["assignees"] = p.getlist("assignees")
        values["hide_from_display"] = bool(p.get("hide_from_display"))
        ctx = _create_ctx(request, values, exc.extra.get("fields", {}), exc.message)
        return render(request, "tasks/task_form.html", ctx, status=422)
    return _done(request, f"/app/tasks/{task.pk}")


# ---------------------------------------------------------------- detail

# action -> (button label, the inputs it asks for)
ACTION_UI: dict[str, tuple[str, tuple[str, ...]]] = {
    "start": ("Start", ()),
    "resume": ("Resume", ()),
    "block": ("I am waiting", ("block_reason", "block_note")),
    "unblock": ("Back to work", ("note",)),
    "submit": ("Mark complete", ("note",)),
    "approve": ("Approve", ()),
    "request_changes": ("Ask for changes", ("feedback",)),
    "remove": ("Remove from task", ("reason",)),
    "cancel": ("Cancel this part", ("reason",)),
}
HIDDEN_ACTIONS = {"pause_eod"}  # done by the system at office closing, never a button


def _actions_for(viewer: Any, a: TaskAssignment) -> list[dict[str, Any]]:
    out = []
    for action in state.allowed_actions(a.status):
        if action in HIDDEN_ACTIONS or not permissions.can(viewer, f"assignment.{action}", a):
            continue
        label, inputs = ACTION_UI.get(action, (action.replace("_", " ").capitalize(), ()))
        out.append({"action": action, "label": label, "inputs": inputs, "danger": action in ("remove", "cancel")})
    return out


def _detail_ctx(
    request: HttpRequest, task: Task, *, message: str = "", errors: dict[str, Any] | None = None,
    open_action: tuple[str, str] | None = None,
) -> dict[str, Any]:  # fmt: skip
    viewer = request.user
    lines = list(task.assignments.select_related("user").order_by("assigned_at"))
    at = now()
    for a in lines:
        a.task = task
        a.status_text = _status_text(viewer, a)
        a.actions = _actions_for(viewer, a) if a.removed_at is None else []
        a.waiting_hours = dashboard.blocked_hours(a, at) if a.status == AssignmentStatus.BLOCKED else 0
    comments = list(task.comments.select_related("author").order_by("created_at"))
    return {
        "page_title": task.task_code,
        "task": task,
        "lines": lines,
        "comments": comments,
        "message": message,
        "errors": errors or {},
        "open_action": open_action,
        "can_manage": permissions.can(viewer, "task.update", task) and not task.is_cancelled,
        "can_comment": permissions.can(viewer, "task.comment", task),
        "block_reasons": [(v, state.BLOCKED_LABELS[BlockReason(v)]) for v in BlockReason.values],
        "overdue": task.due_at < at and not task.is_cancelled and any(a.status in OPEN_STATUSES for a in lines),
        "can_add": permissions.can(viewer, "assignment.add", task) and not task.is_cancelled,
        "addable": selectors.assignable_users(viewer) if permissions.can(viewer, "assignment.add", task) and not task.is_cancelled else [],
        "priorities": Priority.choices,
        "due_local": task.due_at.astimezone(IST).strftime("%Y-%m-%dT%H:%M"),
    }  # fmt: skip


@login_required
@require_http_methods(["GET"])
def task_detail(request: HttpRequest, task_id: str) -> HttpResponse:
    task = _task_or_404(request, task_id)
    return render(request, "tasks/task_detail.html", _detail_ctx(request, task))


def _rerender(request: HttpRequest, task_id: Any, exc: OtsError, open_action: tuple[str, str] | None = None) -> HttpResponse:
    task = _task_or_404(request, task_id)
    ctx = _detail_ctx(request, task, message=exc.message, errors=exc.extra.get("fields", {}), open_action=open_action)
    return render(request, "tasks/task_detail.html", ctx, status=422)


@login_required
@idempotency_optional
@require_http_methods(["POST"])
def assignment_action(request: HttpRequest, assignment_id: str, action: str) -> HttpResponse:
    pk = _uuid(assignment_id)
    a = TaskAssignment.objects.select_related("task").filter(pk=pk).first() if pk else None
    if a is None or action not in state.TRANSITIONS or action in HIDDEN_ACTIONS:
        raise NotFound("Not found.")
    _task_or_404(request, a.task_id)  # a person who cannot see the task cannot act on it
    p: Any = request.POST
    try:
        services.transition(
            a.pk, action, request.user, _version(p.get("version")),
            **{k: p.get(k, "") for k in ("block_reason", "block_note", "note", "feedback", "reason")},
        )  # fmt: skip
    except ValidationFailed as exc:
        return _rerender(request, a.task_id, exc, (str(a.pk), action))
    return _done(request, f"/app/tasks/{a.task_id}")


def _task_post(request: HttpRequest, task_id: str, run: Any) -> HttpResponse:
    task = _task_or_404(request, task_id)
    try:
        run(task, request.POST)
    except ValidationFailed as exc:
        return _rerender(request, task.pk, exc)
    return _done(request, f"/app/tasks/{task.pk}")


@login_required
@idempotency_optional
@require_http_methods(["POST"])
def task_update(request: HttpRequest, task_id: str) -> HttpResponse:
    def run(task: Task, p: Any) -> None:
        changes: dict[str, Any] = {
            "title": p.get("title", ""), "description": p.get("description", ""),
            "priority": p.get("priority", ""), "category": p.get("category", ""),
            "due_at": local_datetime(p.get("due_at", ""), "due_at", required=True),
        }  # fmt: skip
        services.update_task(request.user, task.pk, _version(p.get("version")), **changes)

    return _task_post(request, task_id, run)


@login_required
@idempotency_optional
@require_http_methods(["POST"])
def task_cancel(request: HttpRequest, task_id: str) -> HttpResponse:
    return _task_post(
        request, task_id,
        lambda t, p: services.cancel_task(request.user, t.pk, p.get("reason", ""), _version(p.get("version"))),
    )  # fmt: skip


@login_required
@idempotency_optional
@require_http_methods(["POST"])
def task_add_people(request: HttpRequest, task_id: str) -> HttpResponse:
    return _task_post(
        request, task_id,
        lambda t, p: services.add_assignees(
            request.user, t.pk, uuid_list(p.getlist("assignees"), "assignees"), _version(p.get("version"))
        ),
    )  # fmt: skip


@login_required
@idempotency_optional
@require_http_methods(["POST"])
def task_comment(request: HttpRequest, task_id: str) -> HttpResponse:
    return _task_post(request, task_id, lambda t, p: services.add_comment(request.user, t.pk, p.get("body", "")))


# ---------------------------------------------------------------- dashboards


@login_required
@require_http_methods(["GET"])
def dashboard_view(request: HttpRequest) -> HttpResponse:
    if request.user.role == Role.EMPLOYEE:
        ctx = {"page_title": "Dashboard", "employee": True, **dashboard.employee_dashboard(request.user)}
    else:
        ctx = {"page_title": "Dashboard", "employee": False, **dashboard.manager_dashboard(request.user)}
        ctx["status_counts"] = [(label, ctx["counts"][v], v) for v, label in DerivedStatus.choices]
    ctx["can_create"] = permissions.can(request.user, "task.create")
    return render(request, "tasks/dashboard.html", ctx)


@login_required
@idempotency_optional
def task_new_or_create(request: HttpRequest) -> HttpResponse:
    """One address for the form (GET) and its submission (POST)."""
    return task_create(request) if request.method == "POST" else task_new(request)
