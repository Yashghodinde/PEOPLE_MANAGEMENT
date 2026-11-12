"""The selfie stage of login (M06 section 1)."""

from __future__ import annotations

from django.conf import settings
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from ots.core.errors import OtsError
from ots.core.idempotency import idempotency_exempt

from . import services, sessions


def _next(user: object) -> str:
    return "/password/change" if getattr(user, "must_change_password", False) else settings.OTS_HOME_URL


@idempotency_exempt  # before login there is no user; the key is checked here against the work session
@require_http_methods(["GET", "POST"])
def selfie_view(request: HttpRequest) -> HttpResponse:
    if request.user.is_authenticated and request.method == "GET":
        return redirect(settings.OTS_HOME_URL)
    user = services.pending_user(request)
    key = None
    if request.method == "POST":
        try:
            key = services.check_key(request.headers.get("Idempotency-Key"))
        except OtsError as exc:
            return JsonResponse(exc.to_dict(), status=exc.status)
        if request.user.is_authenticated:  # the same submission arrived twice: answer with the first result
            done = services.existing_for_key(request.user, key)
            if done is not None:
                return JsonResponse({"ok": True, "next": _next(request.user)})
            return JsonResponse({"error": "login_expired", "message": "Log in again."}, status=409)
    if user is None:
        if request.method == "POST":
            return JsonResponse(
                {"error": "login_expired", "message": "That took too long. Log in again."}, status=401
            )
        return redirect("/login")
    if request.method == "GET":
        return render(request, "attendance/selfie.html", {"person": user})
    if request.POST.get("camera") == "unavailable":
        services.selfie_unavailable(request, user)
        return JsonResponse(
            {
                "error": "camera_required",
                "message": "Camera required for attendance. Contact the administrator.",
            },
            status=403,
        )
    upload = request.FILES.get("selfie")
    idle_mode = "OS" if request.POST.get("idle_mode") == "OS" else "TAB_ONLY"
    try:
        services.start_session(
            request,
            user,
            upload.read() if upload else b"",
            device_id=request.POST.get("device_id", ""),
            key=key,
            idle_mode=idle_mode,
        )
    except OtsError as exc:
        return JsonResponse(exc.to_dict(), status=exc.status)
    return JsonResponse({"ok": True, "next": _next(user)})


# ---- what the open page reports (M06 section 2) ----


@require_http_methods(["POST"])
def idle_view(request: HttpRequest) -> JsonResponse:
    """The browser says the person went idle or came back. Only the state is read; its clock never is."""
    if not request.user.is_authenticated:
        return JsonResponse({"error": "login_required"}, status=401)
    try:
        out = sessions.report_state(request.session.session_key or "", request.POST.get("state", ""))
    except OtsError as exc:
        return JsonResponse(exc.to_dict(), status=exc.status)
    return JsonResponse(out)


@require_http_methods(["POST"])
def mode_view(request: HttpRequest) -> JsonResponse:
    if not request.user.is_authenticated:
        return JsonResponse({"error": "login_required"}, status=401)
    try:
        sessions.set_mode(request.session.session_key or "", request.POST.get("idle_mode", ""))
    except OtsError as exc:
        return JsonResponse(exc.to_dict(), status=exc.status)
    return JsonResponse({"ok": True})
