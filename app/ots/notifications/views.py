"""The bell page and the actions on it. Live refresh arrives with M10."""

from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from ots.core.idempotency import idempotency_optional

from . import selectors, services


@login_required
@require_http_methods(["GET"])
def bell(request: HttpRequest) -> HttpResponse:
    return render(
        request,
        "notifications/list.html",
        {"page_title": "Notifications", "items": selectors.bell_items(request.user)},
    )


@login_required
@idempotency_optional
@require_http_methods(["POST"])
def open_notification(request: HttpRequest, notification_id: str) -> HttpResponse:
    n = services.open_one(request.user, notification_id)
    if n and n.link.startswith("/") and not n.link.startswith("//"):
        return redirect(n.link)
    return redirect(f"/app/tasks/{n.task_id}" if n and n.task_id else "/app/notifications")


@login_required
@idempotency_optional
@require_http_methods(["POST"])
def read_all(request: HttpRequest) -> HttpResponse:
    services.mark_all_read(request.user)
    return redirect("/app/notifications")


@login_required
@require_http_methods(["GET"])
def badge(request: HttpRequest) -> HttpResponse:
    """The bell on its own, so a page can refresh the unread count without reloading."""
    return render(request, "components/bell.html")
