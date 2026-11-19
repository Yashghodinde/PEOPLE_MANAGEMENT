"""Task screens (M07.5): list, detail, create form, the action forms, comments and the dashboards.

Views stay thin: services do every write. Forms are plain HTML posts (they work without JavaScript), so the
POST views accept a missing Idempotency-Key; a key that is sent is still enforced."""

from __future__ import annotations

import datetime
import json
import uuid
from typing import Any
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Prefetch, Q
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.utils.html import format_html
from django.views.decorators.http import require_http_methods

from ots.accounts.hr import hr_dashboard
from ots.accounts.models import Role
from ots.attendance import selectors as attendance_selectors
from ots.attendance import sessions as attendance_sessions
from ots.core import permissions
from ots.core.clock import IST, now
from ots.core.errors import NotFound, OtsError, ValidationFailed
from ots.core.idempotency import idempotency_optional
from ots.notifications import selectors as notes
from ots.notifications import services as note_services
from ots.ui.templatetags.ots_ui import dot

from . import cards, chat, dashboard, receipts, selectors, services, state, support
from .models import (
    OPEN_STATUSES,
    AssignmentStatus,
    BlockReason,
    DerivedStatus,
    Priority,
    SupportKind,
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
    task = (
        Task.objects.visible_to(request.user).select_related("created_by").filter(pk=pk).first()
        if pk
        else None
    )
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


# ---------------------------------------------------------------- the card landing (IMP-09)

ACCENT_LABELS = {
    "overdue": "Overdue", "soon": "Due soon", "urgent": "Urgent", "high": "High", "normal": "", "low": "Low",
    "done": "Done",
}  # fmt: skip
CHIPS = [
    ("open", "Open"), ("all", "All"), ("pending", "Pending"), ("progress", "In progress"),
    ("waiting", "Waiting"), ("changes", "Needs changes"), ("submitted", "Submitted"),
    ("completed", "Completed"), ("overdue", "Overdue"),
]  # fmt: skip


def safe_back(raw: str) -> str:
    """A "back to where I was" address from ?from=. Only /app/tasks itself, or a page under it, is accepted;
    anything that could leave the site or climb out of the task pages (another host, //host, a scheme, a
    backslash, ".." segments, control characters) is ignored."""
    raw = (raw or "").strip()
    path = raw.split("?", 1)[0]
    if path != "/app/tasks" and not path.startswith("/app/tasks/"):
        return ""
    if "\\" in raw or "://" in raw or ".." in path or len(raw) > 300:
        return ""
    if any(ord(c) < 32 for c in raw):
        return ""
    return raw


def _decorate(viewer: Any, rows: list[TaskAssignment], shown_to: str) -> list[TaskAssignment]:
    """What each task card needs to draw itself, added to the rows (no further queries)."""
    at = now()
    blocked = [a.pk for a in rows if a.status == AssignmentStatus.BLOCKED]
    answered_help = support.answered_assignment_ids(blocked) if blocked else set()
    unread = cards.unread_by_task(viewer, [a.task_id for a in rows])
    dots = notes.dots_for(viewer, [a.task_id for a in rows])
    for a in rows:
        a.accent = cards.accent(a, at)
        a.accent_label = ACCENT_LABELS[a.accent]
        a.help_answered = a.pk in answered_help
        a.tone = cards.card_tone(a)
        a.status_text = _status_text(viewer, a)
        a.due_text = a.task.due_at.astimezone(IST).strftime("%d %b, %H:%M")
        a.dot = dots.get(str(a.task_id), "")
        u = unread.get(a.task_id, {})
        a.unread_text, a.unread_voice = u.get("text", 0), u.get("voice", 0)
        a.who = a.user if shown_to == "assignee" else a.assigned_by
    return rows


def _remember_view(request: HttpRequest, page: str) -> str:
    """The Tasks page has two views, cards and a table. `?view=` is an explicit choice and is remembered for
    this person; opening the page without it uses what they chose last. Writes only the one column."""
    from ots.accounts.models import User

    asked = request.GET.get("view")
    if asked in ("cards", "table"):
        if asked != request.user.task_view:  # type: ignore[union-attr]
            User.objects.filter(pk=request.user.pk).update(task_view=asked)
            request.user.task_view = asked  # type: ignore[union-attr]
        return asked
    return str(request.user.task_view) if page == "cards" else page  # type: ignore[union-attr]


@login_required
@require_http_methods(["GET"])
def task_landing(request: HttpRequest) -> HttpResponse:
    """/app/tasks: cards for the people who gave me work and the people I gave work to."""
    permissions.require(request.user, "task.view")
    viewer: Any = request.user
    if _remember_view(request, "cards") == "table":  # they last chose the table: open it, as they left it
        keep = {k: v for k, v in request.GET.items() if k != "view"}
        return redirect("/app/tasks/all" + (f"?{urlencode(keep)}" if keep else ""))
    wider = viewer.role in (Role.SUPER_ADMIN, Role.SUB_ADMIN)
    team = wider and request.GET.get("team") == "1"
    to_me = cards.with_unread(cards.assigned_to_me_groups(viewer), cards.unread_by_assigner(viewer))
    mine_or_team = cards.team_groups(viewer) if team else cards.assigned_by_me_groups(viewer)
    by_me = cards.with_unread(mine_or_team, cards.unread_by_assignee(viewer))
    review = _decorate(viewer, cards.review_queue(viewer), "assignee")
    for c in to_me:
        c.href = f"/app/tasks/by/{c.user.pk}"
    for c in by_me:
        c.href = f"/app/tasks/to/{c.user.pk}" + ("?team=1" if team else "")
    return render(
        request,
        "tasks/landing.html",
        {
            "page_title": "Tasks",
            "to_me": to_me,
            "by_me": by_me,
            "review": review,
            "can_create": permissions.can(viewer, "task.create"),
            "wider": wider,
            "team": team,
            "team_label": "Everyone" if viewer.role == Role.SUPER_ADMIN else "My team",
            "show_by_me": wider,
        },
    )


@login_required
@require_http_methods(["GET"])
def task_group_by(request: HttpRequest, user_id: str) -> HttpResponse:
    return _task_group(request, "by", user_id)


@login_required
@require_http_methods(["GET"])
def task_group_to(request: HttpRequest, user_id: str) -> HttpResponse:
    return _task_group(request, "to", user_id)


def _task_group(request: HttpRequest, kind: str, user_id: str) -> HttpResponse:
    """/app/tasks/by/<id> (what that person gave me) and /app/tasks/to/<id> (what I gave them)."""
    permissions.require(request.user, "task.view")
    viewer: Any = request.user
    other = cards.other_user_or_none(user_id)
    team = request.GET.get("team") == "1" and kind == "to"
    if other is None or not cards.has_relationship(viewer, kind, other, team):
        raise NotFound("Not found.")  # the same answer for "no such person" and "nothing in common"
    g = request.GET
    page = cards.group_page(
        viewer, kind, other, chip=g.get("filter", cards.DEFAULT_FILTER), q=g.get("q", ""),
        after=g.get("after", ""), team=team,
    )  # fmt: skip
    rows = _decorate(viewer, page.rows, "assignee" if kind == "to" else "assigner")
    here = request.get_full_path()
    base = {"team": "1"} if team else {}
    chips = [
        {"key": k, "label": label, "count": page.counts.get(k, 0), "on": k == page.filter,
         "url": "?" + urlencode({**base, "filter": k, **({"q": page.q} if page.q else {})})}
        for k, label in CHIPS
    ]  # fmt: skip
    more_url = (
        "?" + urlencode({**base, "filter": page.filter, "q": page.q, "after": page.more_after})
        if page.more_after
        else ""
    )
    title = f"Given to {other.full_name}" if kind == "to" else f"Given to me by {other.full_name}"
    return render(
        request,
        "tasks/group.html",
        {
            "page_title": title,
            "page": page,
            "other": other,
            "kind": kind,
            "rows": rows,
            "chips": chips,
            "more_url": more_url,
            "from_url": here,
            "team": team,
            "crumbs": [("Tasks", "/app/tasks"), (title, "")],
        },
    )


@login_required
@require_http_methods(["GET"])
def task_list(request: HttpRequest) -> HttpResponse:  # /app/tasks/all: the flat table
    permissions.require(request.user, "task.view")
    _remember_view(request, "table")
    g = request.GET
    qs = (
        Task.objects.visible_to(request.user)
        .select_related("created_by")
        .prefetch_related(_live_assignments())
    )
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
    dots = notes.dots_for(request.user, [t.pk for t in page])
    rows = []
    for t in page:
        names = [a.user.full_name for a in t.live]
        who = ", ".join(names[:3]) + (f" +{len(names) - 3}" if len(names) > 3 else "")
        late = t.due_at < at and t.derived_status not in (DerivedStatus.COMPLETED, DerivedStatus.CANCELLED)
        due_text = t.due_at.astimezone(IST).strftime("%d %b, %H:%M")
        rows.append([
            format_html(
                '{}<a class="text-accent" href="/app/tasks/{}">{}</a>', dot(dots, t.pk), t.pk, t.task_code
            ),
            t.title,
            t.get_priority_display(),
            who,
            format_html('<span class="{}">{}</span>', "text-bad" if late else "", due_text),
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


def _create_ctx(
    request: HttpRequest, values: dict[str, Any], errors: dict[str, Any], message: str = ""
) -> dict[str, Any]:
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
        values = {
            k: p.get(k, "")
            for k in ("title", "description", "priority", "category", "due_at", "scheduled_start_at")
        }
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
    "block": ("I'm waiting", ("block_reason", "block_note")),
    "unblock": ("Back to work", ("note",)),
    "submit": ("Submit task for review", ("note",)),
    "approve": ("Approve task", ()),
    "request_changes": ("Request changes", ("feedback",)),
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
        out.append(
            {"action": action, "label": label, "inputs": inputs, "danger": action in ("remove", "cancel")}
        )
    return out


PRIMARY_ACTIONS = ("start", "resume", "submit", "approve")


def _split_actions(actions: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Each action has exactly one home: moving the work along goes in the action bar (the main button last),
    removing or cancelling a part goes in the People panel."""
    workflow = [x for x in actions if not x["danger"]]
    workflow.sort(key=lambda x: x["action"] in PRIMARY_ACTIONS)
    for x in workflow:
        x["primary"] = x["action"] in PRIMARY_ACTIONS
    return workflow, [x for x in actions if x["danger"]]


def _detail_ctx(
    request: HttpRequest, task: Task, *, message: str = "", errors: dict[str, Any] | None = None,
    open_action: tuple[str, str] | None = None,
) -> dict[str, Any]:  # fmt: skip
    viewer = request.user
    lines = list(task.assignments.select_related("user").order_by("assigned_at"))
    at = now()
    marks = receipts.marks_for(viewer, task)  # the sender's ticks: sent, delivered, seen, not delivered yet
    for a in lines:
        a.task = task
        a.mark = marks.get(a.user_id)
        a.status_text = _status_text(viewer, a)
        a.actions = _actions_for(viewer, a) if a.removed_at is None else []
        a.wf_actions, a.admin_actions = _split_actions(a.actions)
        a.waiting_hours = dashboard.blocked_hours(a, at) if a.status == AssignmentStatus.BLOCKED else 0
    comments = list(task.comments.select_related("author").order_by("created_at"))
    overdue = task.due_at < at and not task.is_cancelled and any(a.status in OPEN_STATUSES for a in lines)
    assignee_ids = {a.user_id for a in lines}
    chat_items = chat.build(viewer, task, comments, assignee_ids)
    waiting_ids = [a.pk for a in lines if a.status == AssignmentStatus.BLOCKED]
    answered = support.answered_assignment_ids(waiting_ids) if waiting_ids else set()
    for a in lines:
        a.help_answered = a.pk in answered
    for m in chat_items:
        sr = m.get("request")
        if sr is not None:
            m["can_answer"] = support.may_answer(viewer, sr) and sr.status in support.OPEN_STATES
            m["can_reply"] = viewer.pk == sr.raised_by_id and sr.status in support.OPEN_STATES
    can_add = permissions.can(viewer, "assignment.add", task) and not task.is_cancelled
    mine = {a.user_id for a in lines if a.user_id == viewer.pk and a.removed_at is None}
    bar_lines = [a for a in lines if a.removed_at is None and (a.wf_actions or a.user_id in mine)]
    return {
        "page_title": task.task_code,
        "task": task,
        "lines": lines,
        "bar_lines": bar_lines,
        "comments": comments,
        "message": message,
        "errors": errors or {},
        "open_action": open_action,
        "can_manage": permissions.can(viewer, "task.update", task) and not task.is_cancelled,
        "can_comment": permissions.can(viewer, "task.comment", task),
        "voice_max_seconds": settings.OTS_VOICE_MAX_SECONDS,
        "voice_max_mb": settings.OTS_VOICE_MAX_MB,
        "block_reasons": [(v, state.BLOCKED_LABELS[BlockReason(v)]) for v in BlockReason.values],
        "overdue": overdue,
        "can_add": can_add,
        "addable": selectors.assignable_users(viewer) if can_add else [],
        "priorities": Priority.choices,
        "chat": chat_items,
        "my_side": chat.side_of(viewer.pk, task, assignee_ids),
        "can_upload": permissions.can(viewer, "file.upload", task) and not task.is_cancelled,
        "default_relation": "SUBMISSION" if viewer.role == "EMPLOYEE" else "DELIVERABLE",
        "max_mb": settings.OTS_UPLOAD_MAX_MB,
        "help_kinds": SupportKind.choices,
        "my_lines": [a for a in lines if a.user_id == viewer.pk and a.removed_at is None],
        "open_request": request.GET.get("request", ""),
        "due_local": task.due_at.astimezone(IST).strftime("%Y-%m-%dT%H:%M"),
    }  # fmt: skip


@login_required
@require_http_methods(["GET"])
def task_detail(request: HttpRequest, task_id: str) -> HttpResponse:
    task = _task_or_404(request, task_id)
    note_services.mark_task_read(request.user, task.pk)  # opening the task clears its dot and bell entries
    receipts.mark_seen(request.user, task)  # and tells whoever sent it that it was seen
    ctx = _detail_ctx(request, task)
    ctx["back_url"] = safe_back(request.GET.get("from", ""))  # "back to where I was", only inside /app/tasks
    return render(request, "tasks/task_detail.html", ctx)


def _rerender(
    request: HttpRequest, task_id: Any, exc: OtsError, open_action: tuple[str, str] | None = None
) -> HttpResponse:
    task = _task_or_404(request, task_id)
    ctx = _detail_ctx(
        request, task, message=exc.message, errors=exc.extra.get("fields", {}), open_action=open_action
    )
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


def _help_post(request: HttpRequest, task: Task, run: Any) -> HttpResponse:
    from django.contrib import messages

    rid = ""
    try:
        rid = str(run().pk)
    except (ValidationFailed, NotFound) as exc:
        messages.error(request, str(exc))
    return redirect(f"/app/tasks/{task.pk}" + (f"?request={rid}" if rid else ""))


@login_required
@idempotency_optional
@require_http_methods(["POST"])
def task_help(request: HttpRequest, task_id: str) -> HttpResponse:
    """ "I am waiting": a structured help request on one of my own assignments."""
    task = _task_or_404(request, task_id)
    p = request.POST
    return _help_post(
        request, task,
        lambda: support.raise_request(
            request.user, p.get("assignment", ""), kind=p.get("kind", ""), what=p.get("what", ""),
            why=p.get("why", ""), impact=p.get("impact", ""), urgent=p.get("urgent") == "1", request=request,
        ),
    )  # fmt: skip


@login_required
@idempotency_optional
@require_http_methods(["POST"])
def task_help_reply(request: HttpRequest, task_id: str, request_id: str) -> HttpResponse:
    task = _task_or_404(request, task_id)
    p = request.POST
    return _help_post(
        request,
        task,
        lambda: support.reply(request.user, request_id, p.get("body", ""), p.get("action", "reply"), request),
    )


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
        request,
        task_id,
        lambda t, p: services.cancel_task(
            request.user, t.pk, p.get("reason", ""), _version(p.get("version"))
        ),
    )


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
    if (
        request.headers.get("X-Requested-With") == "ots-chat"
    ):  # the chat composer sends without leaving the page
        task = _task_or_404(request, task_id)
        try:
            c = services.add_comment(request.user, task.pk, request.POST.get("body", ""))
        except ValidationFailed as exc:
            return JsonResponse({"ok": False, "message": str(exc)}, status=422)
        return JsonResponse({"ok": True, "id": f"c-{c.pk}"})
    return _task_post(
        request, task_id, lambda t, p: services.add_comment(request.user, t.pk, p.get("body", ""))
    )


@login_required
@idempotency_optional
@require_http_methods(["POST"])
def task_voice(request: HttpRequest, task_id: str) -> JsonResponse:
    """Called by the recorder after its upload finished. A repeat gets the same message."""
    task = _task_or_404(request, task_id)
    version = _uuid(request.POST.get("version_id"))
    if version is None:
        raise ValidationFailed("Which recording?", fields={"version_id": ["Required."]})
    try:
        seconds = int(request.POST.get("seconds", "0"))
    except ValueError:
        raise ValidationFailed("Say how long it is.", fields={"seconds": ["Invalid."]}) from None
    comment = services.add_voice_comment(request.user, task.pk, version, seconds)
    return JsonResponse(
        {"comment_id": str(comment.pk), "seconds": comment.voice_seconds, "body": comment.body}
    )


# ---------------------------------------------------------------- dashboards


@login_required
@require_http_methods(["GET"])
def dashboard_view(request: HttpRequest) -> HttpResponse:
    if request.user.role == Role.HR:  # people and attendance only: HR has no tasks
        return render(request, "tasks/hr_dashboard.html", hr_dashboard(request.user))
    if request.user.role == Role.EMPLOYEE:
        ctx = {"page_title": "Dashboard", "employee": True, **dashboard.employee_dashboard(request.user)}
    else:
        ctx = {"page_title": "Dashboard", "employee": False, **dashboard.manager_dashboard(request.user)}
        ctx["status_counts"] = [(label, ctx["counts"][v], v) for v, label in DerivedStatus.choices]
    if request.user.role != Role.EMPLOYEE:
        ctx["team_today"] = attendance_selectors.team_today(request.user)
        ctx["undelivered"] = receipts.undelivered(request.user)
    fresh = notes.unread_tasks(request.user)
    shown = {a.task_id for key in ("waiting", "recent_submissions", "today", "paused", "due_soon")
             for a in ctx.get(key, [])} | {t.pk for t in fresh}  # fmt: skip
    ctx["dots"] = notes.dots_for(request.user, shown)
    ctx["fresh"] = fresh
    ctx["can_create"] = permissions.can(request.user, "task.create")
    return render(request, "tasks/dashboard.html", ctx)


@login_required
@idempotency_optional
def task_new_or_create(request: HttpRequest) -> HttpResponse:
    """One address for the form (GET) and its submission (POST)."""
    return task_create(request) if request.method == "POST" else task_new(request)


# ---------------------------------------------------------------- end of day


def _eod_choices(p: Any, lines: list[TaskAssignment]) -> dict[str, dict[str, str]]:
    keys = ("choice", "note", "block_reason", "block_note")
    return {str(a.pk): {k: p.get(f"{k}_{a.pk}", "").strip() for k in keys} for a in lines}


@login_required
@idempotency_optional
@require_http_methods(["GET", "POST"])
def day_close(request: HttpRequest) -> HttpResponse:
    """'Log out for the day': one choice for every piece of open work, applied together, then log out."""
    lines = selectors.eod_lines(request.user)
    for a in lines:
        a.key = str(a.pk)  # type: ignore[attr-defined]  (the template looks errors up by text id)
    ctx: dict[str, Any] = {
        "page_title": "Log out for the day",
        "lines": lines,
        "block_reasons": [(v, state.BLOCKED_LABELS[BlockReason(v)]) for v in BlockReason.values],
        "values": {},
        "errors": {},
    }
    if request.method == "POST":
        choices = _eod_choices(request.POST, lines)
        try:
            services.close_day(request.user, choices)
        except ValidationFailed as exc:
            ctx.update(values=choices, errors=exc.extra.get("fields", {}), message=exc.message)
            return render(request, "tasks/day_close.html", ctx, status=422)
        attendance_sessions.end_current(request.session.session_key or "", "END_OF_DAY")
        logout(request)
        return redirect("/login")
    return render(request, "tasks/day_close.html", ctx)


@login_required
@idempotency_optional
@require_http_methods(["POST"])
def resume_all(request: HttpRequest) -> HttpResponse:
    services.resume_paused(request.user)
    return _done(request, "/app/dashboard")


@login_required
@idempotency_optional
@require_http_methods(["POST"])
def receipts_ack(request: HttpRequest) -> JsonResponse:
    """The page of the person a change was sent to says it arrived (D-052). Time is the server's."""
    try:
        data = (
            json.loads(request.body or b"{}") if request.content_type == "application/json" else request.POST
        )
    except ValueError:
        raise ValidationFailed("The request is not valid JSON.") from None
    raw = data.get("ids") if request.content_type == "application/json" else request.POST.getlist("ids")
    return JsonResponse({"acked": receipts.ack(request.user, raw, request)})


@login_required
@require_http_methods(["GET"])
def receipts_pending(request: HttpRequest) -> JsonResponse:
    """What this person has not acknowledged yet. After a reconnect the page asks, then acks."""
    return JsonResponse({"ids": receipts.pending_ids(request.user)})
