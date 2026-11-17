"""The office TV: pairing, the board, and the Super Admin's page for managing displays (M10 section 3)."""

from __future__ import annotations

import uuid
from typing import Any

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from ots.core import permissions
from ots.core.errors import NotFound, ValidationFailed
from ots.core.idempotency import idempotency_optional

from . import board, displays
from .models import DisplayDevice


def _device(request: HttpRequest) -> DisplayDevice | None:
    return displays.authenticate(request.COOKIES.get(displays.COOKIE))


@never_cache
@require_http_methods(["GET", "POST"])
def pair_page(request: HttpRequest) -> HttpResponse:
    """The TV types the code it was shown. No login: the code is the credential, and it works once."""
    if request.method == "POST":
        try:
            _, token = displays.pair(request.POST.get("code", ""), request)
        except (ValidationFailed, displays.TooManyAttempts) as exc:
            return render(
                request,
                "display/pair.html",
                {"error": str(exc)},
                status=exc.status if hasattr(exc, "status") else 422,
            )
        resp = redirect("/display")
        resp.set_cookie(
            displays.COOKIE, token, max_age=displays.COOKIE_MAX_AGE, httponly=True,
            secure=settings.SESSION_COOKIE_SECURE, samesite="Strict",
        )  # fmt: skip
        return resp
    return render(request, "display/pair.html", {})


@never_cache
@require_http_methods(["GET"])
def board_page(request: HttpRequest) -> HttpResponse:
    if _device(request) is None:
        resp = redirect("/display/pair")
        resp.delete_cookie(displays.COOKIE)
        return resp
    return render(request, "display/board.html", board.build())


@never_cache
@require_http_methods(["GET"])
def board_fragment(request: HttpRequest) -> HttpResponse:
    if _device(request) is None:
        return HttpResponse(status=401)  # the page sends the TV back to the pairing screen
    return render(request, "display/_board.html", board.build())


# --- the Super Admin's side ---


def _admin_ctx(**extra: Any) -> dict[str, Any]:
    return {
        "page_title": "Displays",
        "devices": displays.devices(),
        "minutes": displays.CODE_MINUTES,
        **extra,
    }


@login_required
@require_http_methods(["GET"])
def displays_admin(request: HttpRequest) -> HttpResponse:
    permissions.require(request.user, "display.manage")
    return render(request, "display/admin.html", _admin_ctx())


@login_required
@idempotency_optional
@require_http_methods(["POST"])
def displays_code(request: HttpRequest) -> HttpResponse:
    name = request.POST.get("name", "")
    try:
        code, row = displays.create_code(request.user, name, request)
    except ValidationFailed as exc:
        return render(request, "display/admin.html", _admin_ctx(error=str(exc)), status=422)
    return render(request, "display/admin.html", _admin_ctx(code=code, code_name=row.name))


@login_required
@idempotency_optional
@require_http_methods(["POST"])
def displays_revoke(request: HttpRequest, device_id: str) -> HttpResponse:
    try:
        pk = uuid.UUID(device_id)
    except ValueError:
        raise NotFound("No such display.") from None
    displays.revoke(request.user, pk, request)
    return redirect("/app/displays")
