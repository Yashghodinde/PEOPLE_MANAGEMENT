"""Task screens. M07.3 has only the create endpoint (so a repeated request can be proven idempotent, F-TK-1);
the list, detail and the create form arrive in M07.5."""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect
from django.views.decorators.http import require_http_methods

from ots.core.clock import IST
from ots.core.errors import ValidationFailed

from . import services


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


@login_required
@require_http_methods(["POST"])
def task_create(request: HttpRequest) -> HttpResponse:
    p: Any = request.POST
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
    return _done(request, f"/app/tasks/{task.pk}")
