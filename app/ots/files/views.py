"""Upload endpoints (M08 section 2). JSON in, JSON out; the browser uploader (M08.3) drives them.

They sit outside /app/ on purpose: the Idempotency-Key lives in the start request's body and the session id
names every later piece, so the generic header middleware is not involved.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_http_methods

from ots.core import permissions
from ots.core.errors import NotFound, ValidationFailed
from ots.tasks.models import Task

from . import download as download_mod
from . import selectors, services
from .models import FileState

_RANGE = re.compile(r"^bytes (\d+)-(\d+)/(\d+)$")


def _uuid(raw: Any, field: str, required: bool = False) -> uuid.UUID | None:
    if raw in (None, ""):
        if required:
            raise ValidationFailed("This is required.", fields={field: ["Required."]})
        return None
    try:
        return uuid.UUID(str(raw))
    except ValueError:
        raise ValidationFailed("That is not valid.", fields={field: ["Invalid."]}) from None


def _body(progress: services.Progress) -> dict[str, Any]:
    s, v = progress.session, progress.version
    out: dict[str, Any] = {
        "upload_id": str(s.pk),
        "piece_size": int(settings.OTS_UPLOAD_PIECE_BYTES),
        "received_bytes": s.received_bytes,
        "size": s.expected_size,
        "state": s.state,
    }
    if v is not None:
        out["version_id"] = str(v.pk)
        out["file_id"] = str(v.file_id)
        out["file_code"] = v.file_code
        out["version"] = v.version_number
        out["archived"] = v.state == FileState.ARCHIVED
    return out


@login_required
@require_http_methods(["POST"])
def uploads(request: HttpRequest) -> JsonResponse:
    try:
        data = json.loads(request.body or b"{}")
        if not isinstance(data, dict):
            raise ValueError
    except ValueError:
        raise ValidationFailed("The request is not valid JSON.") from None
    task = None
    task_id = _uuid(data.get("task_id"), "task_id")
    if task_id is not None:
        task = Task.objects.visible_to(request.user).filter(pk=task_id).first()
        if task is None:
            raise NotFound("No such task.")
    try:
        size = int(data.get("size"))
    except (TypeError, ValueError):
        raise ValidationFailed("The size is not valid.", fields={"size": ["Invalid."]}) from None
    session, created = services.start_upload(
        request.user,  # type: ignore[arg-type]
        key=_uuid(data.get("idempotency_key"), "idempotency_key", required=True),  # type: ignore[arg-type]
        task=task,
        file_id=_uuid(data.get("file_id"), "file_id"),
        assignment_id=_uuid(data.get("assignment_id"), "assignment_id"),
        relation=str(data.get("relation") or "SUBMISSION"),
        original_filename=str(data.get("original_filename") or ""),
        display_name=str(data.get("display_name") or ""),
        size=size,
        sha256=str(data.get("sha256") or ""),
        allow_blocked=data.get("allow_blocked") is True,
        request=request,
    )
    progress = services.get_session(request.user, session.pk)  # type: ignore[arg-type]
    return JsonResponse(_body(progress), status=201 if created else 200)


@login_required
@require_http_methods(["GET", "PUT", "DELETE"])
def upload_detail(request: HttpRequest, upload_id: uuid.UUID) -> JsonResponse:
    user: Any = request.user
    if request.method == "GET":
        return JsonResponse(_body(services.get_session(user, upload_id)))
    if request.method == "DELETE":
        services.cancel_upload(user, upload_id)
        return JsonResponse({"upload_id": str(upload_id), "state": FileState.ABANDONED})
    match = _RANGE.match(request.headers.get("Content-Range", ""))
    if not match:
        raise ValidationFailed("Send a Content-Range header like 'bytes 0-8388607/52428800'.")
    start, end, total = (int(g) for g in match.groups())
    progress = services.receive_piece(user, upload_id, start, end, total, request, request=request)  # type: ignore[arg-type]
    return JsonResponse(_body(progress), status=201 if progress.version else 200)


@login_required
@require_http_methods(["GET"])
def task_files_panel(request: HttpRequest, task_id: uuid.UUID) -> HttpResponse:
    """The files section of a task page. It is loaded after the page, and reloads itself while any file is
    not yet archived (so "Not yet off-site" clears without the person doing anything)."""
    task = Task.objects.visible_to(request.user).filter(pk=task_id).first()
    if task is None:
        raise NotFound("Task not found.")
    rows = selectors.task_file_rows(request.user, task)
    can_upload = permissions.can(request.user, "file.upload", task) and not task.is_cancelled
    employee_only = can_upload and request.user.role == "EMPLOYEE"  # type: ignore[union-attr]
    return render(
        request,
        "files/_list.html" if request.GET.get("list") else "files/_panel.html",
        {
            "task": task,
            "rows": rows,
            "pending": selectors.anything_pending(rows),
            "can_upload": can_upload,
            "can_attach": permissions.can(request.user, "file.attach_reference", task)
            and not task.is_cancelled,
            "default_relation": "SUBMISSION" if employee_only else "DELIVERABLE",
            "relations": []
            if employee_only
            else [("DELIVERABLE", "Deliverable"), ("REFERENCE", "Reference"), ("SUBMISSION", "Submission")],
            "max_mb": settings.OTS_UPLOAD_MAX_MB,
        },
    )


@login_required
@require_http_methods(["GET"])
def download(request: HttpRequest, version_id: uuid.UUID) -> HttpResponse:
    return download_mod.serve(request, version_id, "DOWNLOAD")


@login_required
@require_http_methods(["GET"])
def preview(request: HttpRequest, version_id: uuid.UUID) -> HttpResponse:
    return download_mod.serve(request, version_id, "PREVIEW")


@login_required
@require_http_methods(["GET", "POST"])
def handover(request: HttpRequest, version_id: uuid.UUID) -> HttpResponse:
    """ "Send file to…": a plain form page that works without JavaScript."""
    from django.shortcuts import redirect

    from . import handover as handover_mod

    version = selectors.versions_visible_to(request.user).select_related("file").filter(pk=version_id).first()
    if version is None:
        raise NotFound("File not found.")
    permissions.require(request.user, "file.handover", version)
    link = version.task_links.select_related("task").order_by("-linked_at").first()
    back = f"/app/tasks/{link.task_id}" if link else "/app/tasks"
    people = list(selectors.handover_targets(request.user).order_by("full_name")[:200])
    ctx: dict[str, Any] = {
        "page_title": "Send file",
        "version": version,
        "people": people,
        "back": back,
        "sent": False,
        "chosen": set(),
    }
    if request.method == "POST":
        chosen = request.POST.getlist("recipients")
        try:
            handover_mod.send(request.user, version_id, chosen, request.POST.get("note", ""), request)
        except ValidationFailed as exc:
            ctx.update(error=str(exc), chosen=set(chosen), note=request.POST.get("note", ""))
            return render(request, "files/handover.html", ctx, status=422)
        return redirect(f"{request.path}?sent=1")
    ctx["sent"] = request.GET.get("sent") == "1"
    return render(request, "files/handover.html", ctx)


@login_required
@require_http_methods(["GET"])
def file_trail(request: HttpRequest, file_id: uuid.UUID) -> HttpResponse:
    rows = selectors.trail(request.user, file_id)
    if rows is None:
        raise NotFound("File not found.")
    from .models import File

    f = File.objects.get(pk=file_id)
    link = f.task_links.order_by("-linked_at").first()
    back = f"/app/tasks/{link.task_id}" if link else "/app/tasks"
    return render(
        request, "files/trail.html", {"page_title": "File history", "file": f, "rows": rows, "back": back}
    )


def _date(raw: str | None) -> Any:
    import datetime

    try:
        return datetime.date.fromisoformat(raw) if raw else None
    except ValueError:
        raise ValidationFailed("That date is not valid.", fields={"date": ["Invalid."]}) from None


@login_required
@require_http_methods(["GET"])
def search(request: HttpRequest) -> HttpResponse:
    g = request.GET
    attach = g.get("attach", "")
    task = Task.objects.visible_to(request.user).filter(pk=attach).first() if _is_uuid(attach) else None
    try:
        page = max(0, int(g.get("page", "0")))
    except ValueError:
        page = 0
    rows = selectors.search(
        request.user,
        g.get("q", ""),
        uploader=g.get("uploader") if _is_uuid(g.get("uploader", "")) else None,
        task=g.get("task") if _is_uuid(g.get("task", "")) else None,
        extension=g.get("ext", "")[:10],
        relation=g.get("relation", "")
        if g.get("relation") in ("REFERENCE", "SUBMISSION", "DELIVERABLE")
        else "",
        state=g.get("state", "")[:20],
        date_from=_date(g.get("from")),
        date_to=_date(g.get("to")),
        limit=selectors.SEARCH_PAGE + 1,
        offset=page * selectors.SEARCH_PAGE,
    )
    more = len(rows) > selectors.SEARCH_PAGE
    can_attach = task is not None and permissions.can(request.user, "file.attach_reference", task)
    return render(
        request,
        "files/search.html",
        {
            "page_title": "Attach a file" if can_attach else "Files",
            "rows": rows[: selectors.SEARCH_PAGE], "q": g.get("q", ""), "page": page, "more": more,
            "attach": task if can_attach else None, "get": g,
        },
    )  # fmt: skip


def _is_uuid(raw: str) -> bool:
    try:
        uuid.UUID(raw)
    except ValueError:
        return False
    return True


@login_required
@require_http_methods(["POST"])
def attach(request: HttpRequest, task_id: uuid.UUID) -> HttpResponse:
    from django.shortcuts import redirect

    from . import library

    version_id = request.POST.get("version", "")
    if not _is_uuid(version_id):
        raise ValidationFailed("Choose a file.", fields={"version": ["Required."]})
    library.attach_existing(request.user, task_id, version_id, request)
    return redirect(f"/app/tasks/{task_id}")


@login_required
@require_http_methods(["GET"])
def library_page(request: HttpRequest) -> HttpResponse:
    raw = request.GET.get("folder", "")
    view = selectors.library_view(raw if _is_uuid(raw) else None)
    if not view:
        raise NotFound("No such folder.")
    view["can_manage"] = permissions.can(request.user, "library.manage", None)
    view["page_title"] = "Library"
    return render(request, "files/library.html", view)


@login_required
@require_http_methods(["POST"])
def library_folder(request: HttpRequest) -> HttpResponse:
    from django.shortcuts import redirect

    from . import library

    parent = request.POST.get("parent") or None
    library.create_folder(request.user, request.POST.get("name", ""), parent, request)
    return redirect(f"/files/library?folder={parent}" if parent else "/files/library")


@login_required
@require_http_methods(["POST"])
def library_move(request: HttpRequest, file_id: uuid.UUID) -> HttpResponse:
    from django.shortcuts import redirect

    from . import library

    folder = request.POST.get("folder") or None
    library.move_file(request.user, file_id, folder, request)
    return redirect(f"/files/library?folder={folder}" if folder else "/files/library")
