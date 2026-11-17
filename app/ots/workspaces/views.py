"""File ID pages (IMP-04): the list, one workspace, and the member and document actions. Every id that is
missing or not the viewer's gives the same 404; the services do the permission checks and the audit."""

from __future__ import annotations

import uuid
from typing import Any

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from ots.accounts.models import Role as UserRole
from ots.accounts.models import User
from ots.core import permissions
from ots.core.errors import NotFound, ValidationFailed

from . import selectors, services
from .models import Category, Role, WorkspaceStatus
from .permissions import effective_role


def _ws(request: HttpRequest, ws_id: uuid.UUID) -> Any:
    ws = selectors.workspaces_visible_to(request.user).filter(pk=ws_id).first()
    if ws is None:
        raise NotFound("Not found.")
    return ws


def _person(raw: str) -> User:
    try:
        pk = uuid.UUID(raw)
    except ValueError:
        raise ValidationFailed("Choose a person.", fields={"user": ["Invalid."]}) from None
    u = User.objects.filter(pk=pk, is_active=True).first()
    if u is None:
        raise ValidationFailed("That person cannot be added.", fields={"user": ["Not allowed."]})
    return u


def _back(ws: Any) -> HttpResponse:
    return redirect(f"/files/ids/{ws.pk}")


def _fail(request: HttpRequest, ws: Any, exc: ValidationFailed) -> HttpResponse:
    return _page(request, ws, error=str(exc), status=422)


@login_required
@require_http_methods(["GET"])
def ids_list(request: HttpRequest) -> HttpResponse:
    if request.user.role == UserRole.HR:
        raise NotFound("Not found.")
    return render(
        request,
        "workspaces/list.html",
        {
            "page_title": "File IDs",
            "cards": selectors.cards(request.user),
            "can_create": permissions.can(request.user, "fileid.create"),
            "categories": Category.choices,
        },
    )


@login_required
@require_http_methods(["POST"])
def ids_create(request: HttpRequest) -> HttpResponse:
    p = request.POST
    try:
        ws = services.create_workspace(
            request.user, name=p.get("name", ""), file_id=p.get("file_id", ""),
            description=p.get("description", ""), category=p.get("category", Category.CLIENT),
            tags=p.get("tags", ""), request=request,
        )  # fmt: skip
    except ValidationFailed as exc:
        return render(
            request,
            "workspaces/list.html",
            {
                "page_title": "File IDs", "cards": selectors.cards(request.user), "can_create": True,
                "categories": Category.choices, "error": str(exc), "form": p,
            },
            status=422,
        )  # fmt: skip
    return _back(ws)


def _page(request: HttpRequest, ws: Any, *, error: str = "", status: int = 200) -> HttpResponse:
    role = effective_role(request.user, ws)
    members = selectors.members(ws)
    taken = {m.user_id for m in members}
    candidates = (
        User.objects.filter(is_active=True, role__in=[UserRole.SUB_ADMIN, UserRole.EMPLOYEE])
        .exclude(pk__in=taken)
        .order_by("full_name")[:300]
        if role == Role.OWNER
        else []
    )
    return render(
        request,
        "workspaces/detail.html",
        {
            "page_title": ws.name, "ws": ws, "role": role, "members": members, "candidates": candidates,
            "documents": selectors.active_documents(ws), "roles": Role.choices,
            "can_contribute": role in (Role.OWNER, Role.CONTRIBUTOR),
            "is_owner": role == Role.OWNER, "archived": ws.status == WorkspaceStatus.ARCHIVED,
            "can_archive": request.user.role == UserRole.SUPER_ADMIN, "max_mb": settings.OTS_UPLOAD_MAX_MB,
            "categories": Category.choices, "error": error, "tags_text": ", ".join(ws.tags),
        },
        status=status,
    )  # fmt: skip


@login_required
@require_http_methods(["GET"])
def ids_detail(request: HttpRequest, ws_id: uuid.UUID) -> HttpResponse:
    return _page(request, _ws(request, ws_id))


@login_required
@require_http_methods(["POST"])
def ids_edit(request: HttpRequest, ws_id: uuid.UUID) -> HttpResponse:
    ws, p = _ws(request, ws_id), request.POST
    try:
        services.edit_details(
            request.user, ws, name=p.get("name", ""), description=p.get("description", ""),
            category=p.get("category", ""), tags=p.get("tags", ""), request=request,
        )  # fmt: skip
    except ValidationFailed as exc:
        return _fail(request, ws, exc)
    return _back(ws)


@login_required
@require_http_methods(["POST"])
def ids_member(request: HttpRequest, ws_id: uuid.UUID) -> HttpResponse:
    """One endpoint, an `op`: add, role, remove, transfer."""
    ws, p = _ws(request, ws_id), request.POST
    op = p.get("op", "")
    try:
        user = _person(p.get("user", ""))
        if op == "add":
            services.add_member(request.user, ws, user, p.get("role", ""), request=request)
        elif op == "role":
            services.change_role(request.user, ws, user, p.get("role", ""), request=request)
        elif op == "remove":
            services.remove_member(request.user, ws, user, request=request)
        elif op == "transfer":
            services.transfer_ownership(request.user, ws, user, request=request)
        else:
            raise ValidationFailed("Choose an action.")
    except ValidationFailed as exc:
        return _fail(request, ws, exc)
    return _back(ws)


@login_required
@require_http_methods(["POST"])
def ids_unlink(request: HttpRequest, ws_id: uuid.UUID, file_id: uuid.UUID) -> HttpResponse:
    from ots.files.models import File

    ws = _ws(request, ws_id)
    file = File.objects.filter(pk=file_id).first()
    if file is None:
        raise NotFound("Not found.")
    services.unlink_document(request.user, ws, file, request=request)
    return _back(ws)


@login_required
@require_http_methods(["POST"])
def ids_status(request: HttpRequest, ws_id: uuid.UUID) -> HttpResponse:
    ws = _ws(request, ws_id)
    status = WorkspaceStatus.ARCHIVED if request.POST.get("status") == "ARCHIVED" else WorkspaceStatus.ACTIVE
    services.set_status(request.user, ws, status, request=request)
    return _back(ws)
