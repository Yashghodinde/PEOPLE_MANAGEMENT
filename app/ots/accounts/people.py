"""People screens (M02.5): list, create/edit, profile, org chart. Views stay thin: services do the writes."""

from __future__ import annotations

import datetime
import uuid
from typing import Any
from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Exists, OuterRef
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.utils.html import format_html
from django.views.decorators.http import require_http_methods

from ots.core import permissions
from ots.core.errors import NotFound, ValidationFailed
from ots.core.idempotency import idempotency_optional

from . import selectors, services
from .models import ROLE_RANK, Department, Qualification, Role, User

PAGE_SIZE = 50


def _uuid_or_none(raw: Any) -> uuid.UUID | None:
    """A UUID from user text, or None when it is blank or not a UUID (never raises)."""
    try:
        return uuid.UUID(str(raw).strip())
    except (ValueError, AttributeError, TypeError):
        return None


def _parse_version(raw: str | None) -> int:
    """The edit form always sends the version it loaded with. Without it the lock protects nobody."""
    try:
        return int((raw or "").strip())
    except ValueError:
        raise ValidationFailed("This form is out of date. Reload the page and try again.") from None


WEEKDAYS = [(0, "Mon"), (1, "Tue"), (2, "Wed"), (3, "Thu"), (4, "Fri"), (5, "Sat"), (6, "Sun")]
ROLE_CHOICES = [(v, label) for v, label in Role.choices]
QUAL_CHOICES = [("", "—"), *[(v, label) for v, label in Qualification.choices]]

# (name, label, type) in the order shown, grouped as in the spec.
GROUPS: list[tuple[str, list[tuple[str, str, str, bool]]]] = [
    ("Identity", [("full_name", "Full name", "text", True), ("employee_id", "Employee ID", "text", True)]),
    (
        "Position",
        [
            ("role", "Role", "select", True),
            ("reports_to", "Reports to", "manager", False),
            ("designation", "Designation", "text", False),
            ("department", "Department", "select", False),
            ("date_of_joining", "Date of joining", "date", False),
        ],
    ),
    (
        "Education",
        [
            ("highest_qualification", "Highest qualification", "select", False),
            ("qualification_detail", "Qualification detail", "text", False),
            ("institution", "Institution", "text", False),
            ("qualification_year", "Year", "number", False),
        ],
    ),
    (
        "Work pattern",
        [
            ("shift_start", "Shift start", "time", False),
            ("shift_end", "Shift end", "time", False),
            ("weekly_off", "Weekly off", "days", False),
        ],
    ),
    ("Contact", [("mobile", "Mobile", "text", False), ("email", "Email", "email", False)]),
]  # (name, label, kind, required) per group, in the order shown


def _done(request: HttpRequest, url: str) -> HttpResponse:
    """HTMX submits get an HX-Redirect; plain submits a normal redirect."""
    if request.headers.get("HX-Request") == "true":
        resp = HttpResponse(status=200)
        resp["HX-Redirect"] = url
        return resp
    return redirect(url)


def _visible_or_404(request: HttpRequest, pk: Any) -> User:
    target = (
        User.objects.visible_to(request.user).select_related("reports_to", "department").filter(pk=pk).first()
    )
    if target is None:
        raise NotFound("Person not found.")
    permissions.require(request.user, "user.view", target)
    return target


# ---------------------------------------------------------------- list


def _page_url(params: Any, number: int) -> str:
    """Link to another page that keeps the current filters."""
    keep = [(k, v) for k, v in params.items() if k != "page" and v != ""]
    return "?" + urlencode([*keep, ("page", number)])


@login_required
@require_http_methods(["GET"])
def people_list(request: HttpRequest) -> HttpResponse:
    permissions.require(request.user, "user.view")
    g = request.GET
    qs = User.objects.visible_to(request.user).select_related("reports_to", "department")
    if g.get("role") in Role.values:
        qs = qs.filter(role=g["role"])
    if g.get("department"):
        if g["department"] == "none":
            qs = qs.filter(department__isnull=True)
        else:
            dept_id = _uuid_or_none(g["department"])
            qs = qs.filter(department_id=dept_id) if dept_id else qs.none()
    if g.get("manager"):
        mgr_id = _uuid_or_none(g["manager"])
        qs = qs.filter(reports_to_id=mgr_id) if mgr_id else qs.none()
    active = g.get("active", "1")
    if active in ("0", "1"):
        qs = qs.filter(is_active=active == "1")
    page = Paginator(qs.order_by("full_name", "id"), PAGE_SIZE).get_page(g.get("page"))
    is_admin = request.user.role != Role.EMPLOYEE
    rows = [
        [
            format_html(
                '<a class="text-accent hover:text-ink-900" href="/app/people/{}">{}</a>', u.pk, u.full_name
            ),
            u.employee_id,
            u.designation or "—",
            u.department.name if u.department else "—",
            u.reports_to.full_name if u.reports_to else "—",
            u.get_role_display(),
            "Active" if u.is_active else "Inactive",
        ]
        for u in page
    ]
    ctx = {
        "page_title": "People",
        "page": page,
        "columns": [
            {"label": c}
            for c in ("Name", "Employee ID", "Designation", "Department", "Manager", "Role", "Status")
        ],
        "rows": rows,
        "f": {
            "role": g.get("role", ""),
            "department": g.get("department", ""),
            "manager": g.get("manager", ""),
            "active": active,
        },
        "roles": ROLE_CHOICES,
        "departments": list(Department.objects.filter(is_active=True).order_by("name")) if is_admin else [],
        "managers": list(
            User.objects.visible_to(request.user)
            .filter(is_active=True, role__in=[Role.SUPER_ADMIN, Role.SUB_ADMIN])
            .order_by("full_name")
        )
        if is_admin
        else [],
        "can_create": permissions.can(request.user, "user.create"),
        "prev_url": _page_url(g, page.previous_page_number()) if page.has_previous() else "",
        "next_url": _page_url(g, page.next_page_number()) if page.has_next() else "",
    }
    return render(request, "accounts/people_list.html", ctx)


# ---------------------------------------------------------------- create / edit


def _manager_choices(role: str, exclude: Any = None) -> list[User]:
    """Valid managers for a role: active and strictly higher rank. Server-filtered; the DB enforces it too."""
    if role not in Role.values:
        return []
    higher = [r for r, k in ROLE_RANK.items() if k > ROLE_RANK[Role(role)]]
    qs = User.objects.filter(is_active=True, role__in=higher, is_break_glass=False).order_by("full_name")
    return list(qs.exclude(pk=exclude) if exclude else qs)


def _parse(post: Any) -> tuple[dict[str, Any], dict[str, list[str]]]:
    """Turn form text into typed values. Returns (values, field errors)."""
    v: dict[str, Any] = {}
    errors: dict[str, list[str]] = {}
    for name in (
        "full_name", "employee_id", "designation", "qualification_detail", "institution", "mobile", "email",
        "role", "highest_qualification",
    ):  # fmt: skip
        if name in post:
            v[name] = post[name].strip()
    for name, parse in (
        ("date_of_joining", datetime.date.fromisoformat),
        ("shift_start", datetime.time.fromisoformat),
        ("shift_end", datetime.time.fromisoformat),
    ):
        raw = post.get(name, "").strip()
        if name in post:
            try:
                v[name] = parse(raw) if raw else None
            except ValueError:
                errors[name] = ["Invalid value."]
    if "qualification_year" in post:
        raw = post["qualification_year"].strip()
        try:
            v["qualification_year"] = int(raw) if raw else None
        except ValueError:
            errors["qualification_year"] = ["Enter a year."]
    if post.get("_form") == "person":  # checkboxes and selects are only meaningful on the full form
        v["weekly_off"] = sorted({int(x) for x in post.getlist("weekly_off") if x in set("0123456")})
        dept = post.get("department", "").strip()
        dept_id = _uuid_or_none(dept)
        v["department"] = Department.objects.filter(pk=dept_id, is_active=True).first() if dept_id else None
        if dept and v["department"] is None:
            errors["department"] = ["Unknown department."]
    return v, errors


def _form_response(request: HttpRequest, ctx: dict[str, Any]) -> HttpResponse:
    """Re-show the form with its errors (422). HTMX gets only the form, so the page does not nest."""
    template = (
        "accounts/_person_form.html"
        if request.headers.get("HX-Request") == "true"
        else "accounts/person_form.html"
    )
    return render(request, template, ctx, status=422)


def _pick_manager(raw: str) -> User | None:
    """The chosen manager as a user, None when blank. Bad or unknown text is a field error, never a 500."""
    raw = (raw or "").strip()
    if not raw:
        return None
    manager_id = _uuid_or_none(raw)
    manager = User.objects.filter(pk=manager_id, is_active=True).first() if manager_id else None
    if manager is None:
        raise ValidationFailed("Choose a valid manager.", fields={"reports_to": ["Unknown manager."]})
    return manager


def _form_ctx(
    request: HttpRequest,
    *,
    target: User | None,
    values: dict[str, Any],
    errors: dict[str, list[str]],
    message: str = "",
) -> dict[str, Any]:
    role = values.get("role") or (target.role if target else Role.EMPLOYEE)
    manager_id = str(
        values.get("reports_to") or (target.reports_to_id if target and target.reports_to_id else "")
    )
    return {
        "page_title": "Edit person" if target else "New person",
        "target": target,
        "groups": GROUPS,
        "v": values,
        "errors": errors,
        "message": message,
        "roles": ROLE_CHOICES,
        "qualifications": QUAL_CHOICES,
        "departments": list(Department.objects.filter(is_active=True).order_by("name")),
        "weekdays": WEEKDAYS,
        "weekly_off": values.get("weekly_off", target.weekly_off if target else [6]),
        "managers": _manager_choices(role, target.pk if target else None),
        "manager_id": manager_id,
        "role": role,
    }


def _initial(t: User) -> dict[str, Any]:
    return {
        "full_name": t.full_name,
        "employee_id": t.employee_id,
        "role": t.role,
        "reports_to": t.reports_to_id or "",
        "designation": t.designation,
        "department": str(t.department_id or ""),
        "date_of_joining": t.date_of_joining or "",
        "highest_qualification": t.highest_qualification,
        "qualification_detail": t.qualification_detail,
        "institution": t.institution,
        "qualification_year": t.qualification_year or "",
        "shift_start": t.shift_start.strftime("%H:%M") if t.shift_start else "",
        "shift_end": t.shift_end.strftime("%H:%M") if t.shift_end else "",
        "weekly_off": t.weekly_off,
        "mobile": t.mobile,
        "email": t.email,
    }


@login_required
@require_http_methods(["GET"])
def manager_options(request: HttpRequest) -> HttpResponse:
    permissions.require(request.user, "user.create")
    role = request.GET.get("role", Role.EMPLOYEE)
    return render(
        request,
        "accounts/_manager_field.html",
        {
            "managers": _manager_choices(role),
            "manager_id": request.GET.get("selected", ""),
            "role": role,
            "errors": {},
        },
    )


@idempotency_optional
@login_required
@require_http_methods(["GET", "POST"])
def person_new(request: HttpRequest) -> HttpResponse:
    permissions.require(request.user, "user.create")
    if request.method == "GET":
        return render(
            request,
            "accounts/person_form.html",
            _form_ctx(request, target=None, values={"role": Role.EMPLOYEE, "weekly_off": [6]}, errors={}),
        )
    values, errors = _parse(request.POST)
    values["reports_to"] = request.POST.get("reports_to", "")
    values["login_id"] = request.POST.get("login_id", "").strip()
    password = request.POST.get("password", "")
    try:
        if errors:
            raise ValidationFailed("Please fix the highlighted fields.", fields=errors)
        manager = _pick_manager(values["reports_to"])
        profile = {
            k: val
            for k, val in values.items()
            if k not in {"full_name", "employee_id", "role", "reports_to", "login_id"}
        }
        user = services.create_user(
            request.user, login_id=values["login_id"], employee_id=values.get("employee_id", ""),
            full_name=values.get("full_name", ""), role=values.get("role", ""), reports_to=manager,
            password=password, request=request, **profile,
        )  # fmt: skip
    except ValidationFailed as exc:
        ctx = _form_ctx(
            request, target=None, values=values, errors=exc.extra.get("fields", {}), message=exc.message
        )
        return _form_response(request, ctx)
    return _done(request, f"/app/people/{user.pk}")


@login_required
@require_http_methods(["GET"])
def person_edit(request: HttpRequest, pk: Any) -> HttpResponse:
    permissions.require(request.user, "user.update")
    target = User.objects.select_related("reports_to").filter(pk=pk).first()
    if target is None:
        raise NotFound("Person not found.")
    return render(
        request,
        "accounts/person_form.html",
        _form_ctx(request, target=target, values=_initial(target), errors={}),
    )


@idempotency_optional
@login_required
@require_http_methods(["POST"])
def person_update(request: HttpRequest, pk: Any) -> HttpResponse:
    """Save an edit. The policy check runs before the row is fetched, so a denied caller learns nothing."""
    permissions.require(request.user, "user.update")
    target = User.objects.visible_to(request.user).select_related("reports_to").filter(pk=pk).first()
    if target is None:
        raise NotFound("Person not found.")
    values, errors = _parse(request.POST)
    values["reports_to"] = request.POST.get("reports_to", "")
    try:
        if errors:
            raise ValidationFailed("Please fix the highlighted fields.", fields=errors)
        expected = _parse_version(request.POST.get("version"))
        manager = _pick_manager(values["reports_to"])
        changes = {k: val for k, val in values.items() if k != "reports_to"}
        # role and manager are saved together, so a Super Admin can be demoted (needs a manager at once)
        # and a promotion gets its higher-ranked manager in the same save
        services.update_profile(
            request.user,
            target,
            expected_version=expected,
            reports_to=services.KEEP if manager is None else manager,
            request=request,
            **changes,
        )
    except ValidationFailed as exc:
        ctx = _form_ctx(
            request, target=target, values=values, errors=exc.extra.get("fields", {}), message=exc.message
        )
        return _form_response(request, ctx)
    return _done(request, f"/app/people/{target.pk}")


# ---------------------------------------------------------------- profile & actions


@login_required
@require_http_methods(["GET"])
def person_profile(request: HttpRequest, pk: Any) -> HttpResponse:
    target = _visible_or_404(request, pk)
    qual = target.get_highest_qualification_display() if target.highest_qualification else ""
    ctx = {
        "page_title": target.full_name,
        "target": target,
        "qualification": qual,
        "tabs": [
            ("work", "Work"),
            ("attendance", "Attendance"),
            ("files", "Files"),
            ("activity", "Activity"),
        ],
        "initial_tab": "work",
        "is_admin": permissions.can(request.user, "user.update", target),
        "is_self": target.pk == request.user.pk,
        "edit_url": f"/app/people/{target.pk}/edit",
        "deactivate_attrs": f'hx-post="/app/people/{target.pk}/deactivate" hx-swap="none" '
        f"hx-vals='{{\"version\": {target.version}}}'",
        "reactivate_attrs": f'hx-post="/app/people/{target.pk}/reactivate" hx-swap="none" '
        f"hx-vals='{{\"version\": {target.version}}}'",
    }
    return render(request, "accounts/person_profile.html", ctx)


def _optional_version(request: HttpRequest) -> int | None:
    """Row actions (deactivate, reactivate, reset) send the version the page showed; bad text is a 422."""
    raw = request.POST.get("version")
    return _parse_version(raw) if raw not in (None, "") else None


def _admin_target(request: HttpRequest, pk: Any, action: str) -> User:
    permissions.require(request.user, action)
    target = User.objects.visible_to(request.user).filter(pk=pk).first()
    if target is None:
        raise NotFound("Person not found.")
    return target


@login_required
@require_http_methods(["POST"])
def person_deactivate(request: HttpRequest, pk: Any) -> HttpResponse:
    target = _admin_target(request, pk, "user.deactivate")
    services.deactivate_user(
        request.user, target, expected_version=_optional_version(request), request=request
    )
    return _done(request, f"/app/people/{target.pk}")


@login_required
@require_http_methods(["POST"])
def person_reactivate(request: HttpRequest, pk: Any) -> HttpResponse:
    target = _admin_target(request, pk, "user.update")
    services.reactivate_user(
        request.user, target, expected_version=_optional_version(request), request=request
    )
    return _done(request, f"/app/people/{target.pk}")


@idempotency_optional
@login_required
@require_http_methods(["POST"])
def person_reset_password(request: HttpRequest, pk: Any) -> HttpResponse:
    target = _admin_target(request, pk, "user.reset_password")
    services.reset_password(
        request.user,
        target,
        request.POST.get("new_password", ""),
        expected_version=_optional_version(request),
        request=request,
    )
    return _done(request, f"/app/people/{target.pk}")


# ---------------------------------------------------------------- org chart


def _node_user(u: User, detail: bool) -> Any:
    """What the org-chart template may see. Outside a Sub-Admin's subtree only the name and id are handed
    over, so a later template edit cannot leak designation, employee ID or anything else by accident."""
    return u if detail else {"pk": u.pk, "full_name": u.full_name}


def _nodes(viewer: User, parent_ids: list[Any]) -> dict[Any, list[dict[str, Any]]]:
    """Children grouped by parent, one query. `detail` is False for people outside a Sub-Admin's subtree."""
    kids = (
        User.objects.filter(reports_to_id__in=parent_ids, is_active=True, is_break_glass=False)
        .annotate(
            has_children=Exists(
                User.objects.filter(reports_to_id=OuterRef("pk"), is_active=True, is_break_glass=False)
            )
        )
        .order_by("full_name")
    )
    own = selectors.subtree_ids(viewer.pk) | {viewer.pk} if viewer.role == Role.SUB_ADMIN else None
    out: dict[Any, list[dict[str, Any]]] = {}
    for k in kids:
        detail = own is None or k.pk in own
        out.setdefault(k.reports_to_id, []).append(
            {"u": _node_user(k, detail), "detail": detail, "has_children": k.has_children, "kids": []}
        )
    return out


@login_required
@require_http_methods(["GET"])
def org_chart(request: HttpRequest) -> HttpResponse:
    permissions.require(request.user, "org.view_chart")
    if request.user.role == Role.SUB_ADMIN:  # a Sub-Admin's chart starts at themselves: own team only
        roots = [request.user]
    else:
        roots = list(
            User.objects.filter(role=Role.SUPER_ADMIN, is_active=True, is_break_glass=False).order_by(
                "full_name"
            )
        )
    own = (
        selectors.subtree_ids(request.user.pk) | {request.user.pk}
        if request.user.role == Role.SUB_ADMIN
        else None
    )
    level2 = _nodes(request.user, [r.pk for r in roots])
    tree = [
        {
            "u": _node_user(r, own is None or r.pk in own),
            "detail": own is None or r.pk in own,
            "has_children": r.pk in level2,
            "kids": level2.get(r.pk, []),
        }
        for r in roots
    ]
    return render(request, "accounts/org_chart.html", {"page_title": "Org chart", "tree": tree})


@login_required
@require_http_methods(["GET"])
def org_children(request: HttpRequest, pk: Any) -> HttpResponse:
    permissions.require(request.user, "org.view_chart")
    if not User.objects.filter(pk=pk, is_active=True, is_break_glass=False).exists():
        raise NotFound("Person not found.")
    if request.user.role == Role.SUB_ADMIN and pk not in selectors.subtree_ids(request.user.pk) | {
        request.user.pk
    }:
        raise NotFound("Person not found.")  # outside their own team: same answer as a missing person
    return render(
        request, "accounts/_org_nodes.html", {"nodes": _nodes(request.user, [pk]).get(pk, []), "nested": True}
    )
