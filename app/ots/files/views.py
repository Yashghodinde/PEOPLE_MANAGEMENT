"""Upload endpoints (M08 section 2). JSON in, JSON out; the browser uploader (M08.3) drives them.

They sit outside /app/ on purpose: the Idempotency-Key lives in the start request's body and the session id
names every later piece, so the generic header middleware is not involved.
"""

from __future__ import annotations

import collections
import json
import re
import time
import uuid
from typing import Any

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_http_methods

from ots.core import permissions
from ots.core.clock import today_local
from ots.core.errors import Conflict, NotFound, ValidationFailed
from ots.tasks.models import Task

from . import download as download_mod
from . import naming, selectors, services
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


_CHECKS: dict[Any, collections.deque[float]] = collections.defaultdict(collections.deque)
CHECKS_PER_MINUTE = 60  # one server process, so a plain in-memory window per person is enough


def _check_allowed(user_id: Any) -> bool:
    at = time.monotonic()
    window = _CHECKS[user_id]
    while window and at - window[0] > 60:
        window.popleft()
    if len(window) >= CHECKS_PER_MINUTE:
        return False
    window.append(at)
    return True


@login_required
@require_http_methods(["POST"])
def check_name(request: HttpRequest) -> JsonResponse:
    """Live feedback on a file name while the person types it. Reads nothing private and writes nothing."""
    if not _check_allowed(request.user.pk):
        return JsonResponse(
            {"error": "rate_limited", "message": "Too many checks. Wait a moment."}, status=429
        )
    try:
        data = json.loads(request.body or b"{}")
        if not isinstance(data, dict):
            raise ValueError
    except ValueError:
        raise ValidationFailed("The request is not valid JSON.") from None
    task = None
    task_id = _uuid(data.get("task_id"), "task_id")
    if task_id is not None:
        task = Task.objects.visible_to(request.user).filter(pk=task_id).first()  # type: ignore[arg-type]
    ctx = naming.NameContext(
        task_code=task.task_code if task else "", task_title=task.title if task else "", today=today_local()
    )
    name = str(data.get("name") or "")[:255]
    verdict = naming.judge(name, ctx)
    same = _same_name(request.user, task, name) if verdict.ok and task is not None else None
    return JsonResponse(
        {"ok": verdict.ok, "message": verdict.message, "suggestion": verdict.suggestion, "same": same}
    )


def _same_name(user: Any, task: Task, name: str) -> dict[str, Any] | None:
    """A document on this task the person can already see with the same name (ignoring capitals): the uploader
    asks whether this is its next version. Never decided for them."""
    from .models import TaskFile

    wanted = " ".join(name.split()).casefold()
    visible = selectors.versions_visible_to(user).filter(is_voice=False).values("pk")
    for link in (
        TaskFile.objects.filter(task=task, version__in=visible)
        .select_related("file", "version")
        .order_by("-linked_at")[:200]
    ):
        if " ".join(link.file.display_name.split()).casefold() == wanted:
            top = (
                link.file.versions.order_by("-version_number")
                .values_list("version_number", flat=True)
                .first()
            )
            return {
                "file_id": str(link.file_id),
                "name": link.file.display_name,
                "next_version": (top or 0) + 1,
            }
    return None


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
    scope_in = data.get("scope") if data.get("scope") in ("personal", "workspace", "library") else ""
    ws = None
    if (
        scope_in == "workspace"
    ):  # the same 404 for a File ID that does not exist and one the person cannot open
        from ots.workspaces.selectors import workspaces_visible_to

        wid = _uuid(data.get("workspace_id"), "workspace_id")
        ws = workspaces_visible_to(request.user).filter(pk=wid).first() if wid else None
        if ws is None:
            raise NotFound("Not found.")
    rel = [p for p in str(data.get("relative_path") or "").replace("\\", "/").split("/") if p][:-1]
    session, created = services.start_upload(
        request.user,  # type: ignore[arg-type]
        key=_uuid(data.get("idempotency_key"), "idempotency_key", required=True),  # type: ignore[arg-type]
        task=task,
        file_id=_uuid(data.get("file_id"), "file_id"),
        assignment_id=_uuid(data.get("assignment_id"), "assignment_id"),
        relation=str(data.get("relation") or "SUBMISSION"),
        original_filename=str(data.get("original_filename") or ""),
        display_name=str(data.get("display_name") or ""),
        scope=scope_in,
        workspace=ws,
        folder_id=_uuid(data.get("folder_id"), "folder_id")
        if scope_in in ("personal", "library", "workspace")
        else None,
        relative_dirs=rel if scope_in in ("personal", "library", "workspace") else None,
        note=str(data.get("note") or "")[:200],
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

    version = (
        selectors.versions_visible_to(request.user)
        .filter(is_voice=False)
        .select_related("file")
        .filter(pk=version_id)
        .first()
    )
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
    from . import browser

    raw = request.GET.get("folder", "")
    m = browser.library(request.user, raw if _is_uuid(raw) else None)
    if not m:
        raise NotFound("No such folder.")
    return manager_response(
        request, m, move_targets=browser.library_move_targets() if m["can_create"] else []
    )


def _back_to_library(folder: Any) -> str:
    return f"/files/library?folder={folder}" if folder else "/files/library"


@login_required
@require_http_methods(["POST"])
def library_folder(request: HttpRequest) -> HttpResponse:
    from . import library

    parent = request.POST.get("parent") or None
    return _folder_post(
        request, lambda: library.create_folder(request.user, request.POST.get("name", ""), parent, request),
        _back_to_library(parent),
    )  # fmt: skip


@login_required
@require_http_methods(["POST"])
def library_folder_rename(request: HttpRequest, folder_id: uuid.UUID) -> HttpResponse:
    from . import library

    name = request.POST.get("name", "")
    return _folder_post(
        request,
        lambda: library.rename_folder(request.user, folder_id, name, request),
        _back_to_library(folder_id),
    )


@login_required
@require_http_methods(["POST"])
def library_move(request: HttpRequest, file_id: uuid.UUID) -> HttpResponse:
    from . import library

    folder = request.POST.get("folder") or None
    return _folder_post(
        request, lambda: library.move_file(request.user, file_id, folder, request), _back_to_library(folder)
    )


@login_required
@require_http_methods(["POST"])
def library_approve(request: HttpRequest, file_id: uuid.UUID) -> HttpResponse:
    from . import library

    return _folder_post(request, lambda: library.approve(request.user, file_id, request), "/files/library")


@login_required
@require_http_methods(["POST"])
def library_reject(request: HttpRequest, file_id: uuid.UUID) -> HttpResponse:
    from . import library

    reason = request.POST.get("reason", "")
    return _folder_post(
        request, lambda: library.reject(request.user, file_id, reason, request), "/files/library"
    )


@login_required
@require_http_methods(["POST"])
def library_folder_uploads(request: HttpRequest, folder_id: uuid.UUID) -> HttpResponse:
    from . import library

    allowed = request.POST.get("allow") == "1"
    return _folder_post(
        request, lambda: library.set_employee_upload(request.user, folder_id, allowed, request),
        _back_to_library(folder_id),
    )  # fmt: skip


@login_required
@require_http_methods(["POST"])
def library_move_item(request: HttpRequest) -> HttpResponse:
    """The file manager's Move dialog: kind, id, to."""
    from . import library

    to = request.POST.get("to") or None
    fid = _uuid(request.POST.get("id", ""), "id", True)
    return _folder_post(
        request, lambda: library.move_file(request.user, fid, to, request), _back_to_library(to)
    )


# ---------------------------------------------------------------- personal storage (IMP-02)


def _personal_ctx(request: HttpRequest, active: str, **extra: Any) -> dict[str, Any]:
    return {"max_mb": settings.OTS_UPLOAD_MAX_MB, **extra}


@login_required
@require_http_methods(["GET"])
def mine(request: HttpRequest) -> HttpResponse:
    from . import browser

    permissions.require(request.user, "file.upload_personal")
    try:
        page = max(0, int(request.GET.get("page", "0")))
    except ValueError:
        page = 0
    return manager_response(request, browser.personal_root(request.user, page))


@login_required
@require_http_methods(["GET"])
def mine_day(request: HttpRequest, day: str) -> HttpResponse:
    import datetime

    from . import browser

    permissions.require(request.user, "file.upload_personal")
    try:
        d = datetime.date.fromisoformat(day)
    except ValueError:
        raise NotFound("No such day.") from None
    return manager_response(request, browser.personal_day(request.user, d))


@login_required
@require_http_methods(["GET"])
def mine_folder(request: HttpRequest, folder_id: uuid.UUID) -> HttpResponse:
    from . import browser, folders

    permissions.require(request.user, "file.upload_personal")
    folder = folders.my_folder(request.user, folder_id)
    return manager_response(request, browser.personal_folder(request.user, folder))


def manager_response(request: HttpRequest, m: dict[str, Any], **extra: Any) -> HttpResponse:
    """Every file section renders through this one page (IMP-14.3)."""
    from . import browser

    m["next"] = request.get_full_path()
    ctx = {
        "m": m, "page_title": m["title"], "crumbs": m["crumbs"], "max_mb": settings.OTS_UPLOAD_MAX_MB,
        "move_targets": browser.move_targets(request.user) if m.get("section") == "mine" else [],
        **extra,
    }  # fmt: skip
    return render(request, "files/manager.html", ctx)


def _folder_post(request: HttpRequest, run: Any, fallback: str) -> HttpResponse:
    """Run a folder write from a form; a refusal comes back as a message on the same page."""
    from django.contrib import messages
    from django.shortcuts import redirect

    try:
        run()
    except (ValidationFailed, Conflict, NotFound) as exc:
        messages.error(request, str(exc))
    nxt = request.POST.get("next") or ""
    return redirect(nxt if nxt.startswith("/files/") and not nxt.startswith("//") else fallback)


@login_required
@require_http_methods(["POST"])
def folder_create(request: HttpRequest) -> HttpResponse:
    from . import folders

    parent = request.POST.get("parent") or None
    day = None
    if request.POST.get("day"):
        import datetime

        try:
            day = datetime.date.fromisoformat(request.POST["day"])
        except ValueError:
            day = None
    return _folder_post(
        request,
        lambda: folders.create_personal(request.user, request.POST.get("name", ""), parent, day, request),
        "/files/mine",
    )


@login_required
@require_http_methods(["POST"])
def folder_rename(request: HttpRequest, folder_id: uuid.UUID) -> HttpResponse:
    from . import folders

    return _folder_post(
        request,
        lambda: folders.rename_personal(request.user, folder_id, request.POST.get("name", ""), request),
        "/files/mine",
    )


@login_required
@require_http_methods(["POST"])
def mine_move(request: HttpRequest) -> HttpResponse:
    """Move one of my files or folders into another of my folders (empty target = under its date / a root)."""
    from . import folders

    kind, item, to = request.POST.get("kind"), request.POST.get("id", ""), request.POST.get("to") or None
    if kind == "folder":
        run = lambda: folders.move_personal(request.user, _uuid(item, "id", True), to, request)  # noqa: E731
    else:
        run = lambda: folders.place_personal_file(request.user, _uuid(item, "id", True), to, request)  # noqa: E731
    return _folder_post(request, run, "/files/mine")


@login_required
@require_http_methods(["GET"])
def shared(request: HttpRequest) -> HttpResponse:
    from . import browser

    return manager_response(request, browser.shared_with_me(request.user))


@login_required
@require_http_methods(["GET"])
def task_storage_page(request: HttpRequest) -> HttpResponse:
    """/files/tasks: the dates, newest first, a week at a time; each opens to its employees or tasks."""
    import datetime

    from . import browser

    try:
        before = (
            datetime.date.fromisoformat(request.GET.get("before", "")) if request.GET.get("before") else None
        )
    except ValueError:
        before = None
    m = browser.task_root(request.user, before)
    m["page_param"] = "before"
    return manager_response(request, m)


@login_required
@require_http_methods(["GET"])
def task_day_page(request: HttpRequest, day: str) -> HttpResponse:
    import datetime

    from . import browser

    try:
        d = datetime.date.fromisoformat(day)
    except ValueError:
        raise NotFound("No such day.") from None
    return manager_response(request, browser.task_day(request.user, d, request.GET.get("p", "")[:120]))


@login_required
@require_http_methods(["GET"])
def task_exchange_page(request: HttpRequest, task_id: uuid.UUID) -> HttpResponse:
    """/files/tasks/<id>: one task's documents, and the exchange timeline."""
    import datetime

    from . import browser, taskstorage

    task = Task.objects.visible_to(request.user).filter(pk=task_id).first()
    if task is None:
        raise NotFound("Task not found.")
    try:
        day = datetime.date.fromisoformat(request.GET.get("day", "")) if request.GET.get("day") else None
    except ValueError:
        day = None
    m = browser.task_exchange(request.user, task, day, request.GET.get("p", "")[:120])
    return manager_response(request, m, timeline=taskstorage.task_timeline(request.user, task))


@login_required
@require_http_methods(["GET"])
def deleted(request: HttpRequest) -> HttpResponse:
    from . import personal

    permissions.require(request.user, "file.restore")
    return render(
        request,
        "files/deleted.html",
        _personal_ctx(request, "deleted", page_title="Hidden personal files", files=personal.hidden_files()),
    )


def _personal_post(request: HttpRequest, run: Any, back: str) -> HttpResponse:
    from django.shortcuts import redirect

    run()
    nxt = request.POST.get("next") or ""
    return redirect(nxt if nxt.startswith("/files/") and not nxt.startswith("//") else back)


@login_required
@require_http_methods(["POST"])
def personal_rename(request: HttpRequest, file_id: uuid.UUID) -> HttpResponse:
    from . import personal

    return _personal_post(
        request,
        lambda: personal.rename(request.user, file_id, request.POST.get("name", ""), request),
        "/files/mine",
    )


@login_required
@require_http_methods(["POST"])
def personal_hide(request: HttpRequest, file_id: uuid.UUID) -> HttpResponse:
    from . import personal

    return _personal_post(request, lambda: personal.hide(request.user, file_id, request), "/files/mine")


@login_required
@require_http_methods(["POST"])
def personal_restore(request: HttpRequest, file_id: uuid.UUID) -> HttpResponse:
    from . import personal

    return _personal_post(request, lambda: personal.restore(request.user, file_id, request), "/files/deleted")
