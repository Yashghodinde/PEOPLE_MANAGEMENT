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
