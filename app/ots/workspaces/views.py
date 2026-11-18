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


def _members_back(request: HttpRequest, ws: Any, message: str = "") -> HttpResponse:
    """After a members action the person stays on the Members tab of the same File ID. The one exception: if
    the action took their own access away there is no page left to show, so they go to their File IDs."""
    from django.contrib import messages

    if message:
        messages.success(request, message)
    if effective_role(request.user, ws) is None:
        return redirect("/files/ids")
    return redirect(f"/files/ids/{ws.pk}?tab=members#members")


def _fail(request: HttpRequest, ws: Any, exc: ValidationFailed) -> HttpResponse:
    return _page(request, ws, error=str(exc), status=422)


@login_required
@require_http_methods(["GET"])
def ids_list(request: HttpRequest) -> HttpResponse:
    from ots.files import browser
    from ots.files.views import manager_response

    if request.user.role == UserRole.HR:
        raise NotFound("Not found.")
    m = browser.workspace_list(request.user, permissions.can(request.user, "fileid.create"))
    return manager_response(request, m)


def _new_form(request: HttpRequest, *, error: str = "", form: Any = None, status: int = 200) -> HttpResponse:
    return render(
        request,
        "workspaces/new.html",
        {"page_title": "New File ID", "categories": Category.choices, "error": error, "form": form or {}},
        status=status,
    )


@login_required
@require_http_methods(["GET"])
def ids_check(request: HttpRequest) -> Any:
    """The create form asks: is this File ID free, or what ID would this name get? Writes nothing."""
    from django.http import JsonResponse

    from . import ids
    from .models import FileIdWorkspace

    permissions.require(request.user, "fileid.create")
    typed = ids.normalise(request.GET.get("file_id", ""))
    if typed:
        if not ids.is_standard(typed):
            return JsonResponse({"ok": False, "id": typed, "message": "Use the form FTS-AZE-483921."})
        taken = FileIdWorkspace.objects.filter(file_id_norm=typed).exists()
        return JsonResponse(
            {"ok": not taken, "id": typed, "message": "Already registered." if taken else "Available."}
        )
    for _ in range(8):
        shown = ids.generate(request.GET.get("name", ""))
        if not FileIdWorkspace.objects.filter(file_id_norm=shown).exists():
            return JsonResponse({"ok": True, "id": shown, "message": "Available."})
    return JsonResponse({"ok": False, "id": "", "message": "Try again."})


@login_required
@require_http_methods(["GET", "POST"])
def ids_create(request: HttpRequest) -> HttpResponse:
    permissions.require(request.user, "fileid.create")
    if request.method == "GET":
        return _new_form(request)
    p = request.POST
    try:
        ws = services.create_workspace(
            request.user, name=p.get("name", ""), file_id=p.get("file_id", ""),
            description=p.get("description", ""), category=p.get("category", Category.CLIENT),
            tags=p.get("tags", ""), request=request, strict_format=True,
        )  # fmt: skip
    except ValidationFailed as exc:
        return _new_form(request, error=str(exc), form=p, status=422)
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
    ws = _ws(request, ws_id)
    if request.GET.get("tab") == "members":
        return _page(request, ws)
    return _files(request, ws, None)


def _files(request: HttpRequest, ws: Any, folder: Any) -> HttpResponse:
    from ots.files import browser
    from ots.files.views import manager_response

    role = effective_role(request.user, ws)
    can_write = role in (Role.OWNER, Role.CONTRIBUTOR) and ws.status != WorkspaceStatus.ARCHIVED
    m = browser.workspace_files(ws, role, can_write, folder)
    targets = browser.workspace_move_targets(ws) if can_write else []
    return manager_response(request, m, move_targets=targets)


@login_required
@require_http_methods(["GET"])
def ids_folder(request: HttpRequest, ws_id: uuid.UUID, folder_id: uuid.UUID) -> HttpResponse:
    from . import folders

    ws = _ws(request, ws_id)
    return _files(request, ws, folders.folder_of(ws, folder_id))


def _folder_write(request: HttpRequest, ws: Any, run: Any) -> HttpResponse:
    """A folder action from a form: a refusal comes back as a message on the same page."""
    from django.contrib import messages

    from ots.core.errors import Conflict

    try:
        run()
    except (ValidationFailed, Conflict, NotFound) as exc:
        messages.error(request, str(exc))
    nxt = request.POST.get("next") or ""
    ok = nxt.startswith(f"/files/ids/{ws.pk}") and not nxt.startswith("//")
    return redirect(nxt if ok else f"/files/ids/{ws.pk}")


@login_required
@require_http_methods(["POST"])
def ids_folder_create(request: HttpRequest, ws_id: uuid.UUID) -> HttpResponse:
    from . import folders

    ws = _ws(request, ws_id)
    name, parent = request.POST.get("name", ""), request.POST.get("parent") or None
    return _folder_write(request, ws, lambda: folders.create(request.user, ws, name, parent, request))


@login_required
@require_http_methods(["POST"])
def ids_folder_rename(request: HttpRequest, ws_id: uuid.UUID, folder_id: uuid.UUID) -> HttpResponse:
    from . import folders

    ws = _ws(request, ws_id)
    name = request.POST.get("name", "")
    return _folder_write(request, ws, lambda: folders.rename(request.user, ws, folder_id, name, request))


@login_required
@require_http_methods(["POST"])
def ids_move(request: HttpRequest, ws_id: uuid.UUID) -> HttpResponse:
    """The Move dialog: kind (file | folder), id, to."""
    from . import folders

    ws = _ws(request, ws_id)
    kind, to = request.POST.get("kind"), request.POST.get("to") or None
    try:
        item = uuid.UUID(request.POST.get("id", ""))
    except ValueError:
        raise ValidationFailed("Choose what to move.") from None
    if kind == "folder":
        return _folder_write(request, ws, lambda: folders.move_folder(request.user, ws, item, to, request))
    return _folder_write(request, ws, lambda: folders.place_document(request.user, ws, item, to, request))


def _physical_page(request: HttpRequest, ws: Any, *, status: int = 200) -> HttpResponse:
    from . import physical

    if not physical.can_see(request.user, ws):
        raise NotFound("Not found.")
    role = effective_role(request.user, ws)
    can_write = role in (Role.OWNER, Role.CONTRIBUTOR) and ws.status != WorkspaceStatus.ARCHIVED
    docs = [d.file for d in selectors.active_documents(ws).select_related("file")[:300]] if can_write else []
    recs = list(physical.records(request.user, ws).prefetch_related("moves__changed_by")[:300])
    return render(
        request,
        "workspaces/physical.html",
        {
            "page_title": f"{ws.name}: physical records", "ws": ws, "role": role, "records": recs,
            "can_write": can_write, "suggest": physical.suggestions(ws), "documents": docs,
            "statuses": physical.PhysicalStatus.choices,
            "crumbs": [("Files", "/files/mine"), ("File IDs", "/files/ids"), (ws.name, f"/files/ids/{ws.pk}"),
                       ("Physical records", "")],
        },
        status=status,
    )  # fmt: skip


@login_required
@require_http_methods(["GET"])
def ids_physical(request: HttpRequest, ws_id: uuid.UUID) -> HttpResponse:
    return _physical_page(request, _ws(request, ws_id))


def _physical_write(request: HttpRequest, ws: Any, run: Any) -> HttpResponse:
    from django.contrib import messages

    try:
        run()
    except ValidationFailed as exc:
        messages.error(request, str(exc))
    return redirect(f"/files/ids/{ws.pk}/physical")


@login_required
@require_http_methods(["POST"])
def ids_physical_create(request: HttpRequest, ws_id: uuid.UUID) -> HttpResponse:
    from . import physical

    ws, p = _ws(request, ws_id), request.POST
    return _physical_write(
        request, ws,
        lambda: physical.create(
            request.user, ws, name=p.get("name", ""), office=p.get("office", ""), block=p.get("block", ""),
            shelf=p.get("shelf", ""), box=p.get("box", ""), category=p.get("category", ""),
            notes=p.get("notes", ""), file_id=p.get("file") or None, request=request,
        ),
    )  # fmt: skip


@login_required
@require_http_methods(["POST"])
def ids_physical_move(request: HttpRequest, ws_id: uuid.UUID, record_id: uuid.UUID) -> HttpResponse:
    from . import physical

    ws, p = _ws(request, ws_id), request.POST
    return _physical_write(
        request, ws,
        lambda: physical.move(
            request.user, ws, record_id, office=p.get("office", ""), block=p.get("block", ""),
            shelf=p.get("shelf", ""), box=p.get("box", ""), reason=p.get("reason", ""), request=request,
        ),
    )  # fmt: skip


@login_required
@require_http_methods(["POST"])
def ids_physical_status(request: HttpRequest, ws_id: uuid.UUID, record_id: uuid.UUID) -> HttpResponse:
    from . import physical

    ws = _ws(request, ws_id)
    status = request.POST.get("status", "")
    return _physical_write(
        request, ws, lambda: physical.set_status(request.user, ws, record_id, status, request)
    )


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
    return _members_back(request, ws, "The details were saved.")


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
            done = f"{user.full_name} was added."
        elif op == "role":
            services.change_role(request.user, ws, user, p.get("role", ""), request=request)
            done = f"The role of {user.full_name} was changed."
        elif op == "remove":
            services.remove_member(request.user, ws, user, request=request)
            done = f"{user.full_name} was removed. Their access has stopped; the files and history stay."
        elif op == "transfer":
            services.transfer_ownership(request.user, ws, user, request=request)
            done = f"{user.full_name} is now the owner."
        else:
            raise ValidationFailed("Choose an action.")
    except ValidationFailed as exc:
        return _fail(request, ws, exc)
    return _members_back(request, ws, done)


@login_required
@require_http_methods(["POST"])
def ids_unlink(request: HttpRequest, ws_id: uuid.UUID, file_id: uuid.UUID) -> HttpResponse:
    from ots.files.models import File

    ws = _ws(request, ws_id)
    file = File.objects.filter(pk=file_id).first()
    if file is None:
        raise NotFound("Not found.")
    services.unlink_document(request.user, ws, file, request=request)
    nxt = request.POST.get("next") or ""
    return redirect(
        nxt if nxt.startswith(f"/files/ids/{ws.pk}") and not nxt.startswith("//") else f"/files/ids/{ws.pk}"
    )


@login_required
@require_http_methods(["POST"])
def ids_status(request: HttpRequest, ws_id: uuid.UUID) -> HttpResponse:
    ws = _ws(request, ws_id)
    status = WorkspaceStatus.ARCHIVED if request.POST.get("status") == "ARCHIVED" else WorkspaceStatus.ACTIVE
    services.set_status(request.user, ws, status, request=request)
    return _members_back(request, ws, "Archived." if status == WorkspaceStatus.ARCHIVED else "Restored.")
