"""The selfie stage of login (M06 section 1)."""

from __future__ import annotations

import datetime
import uuid

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import FileResponse, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from ots.accounts.models import User
from ots.audit import api as audit
from ots.core import permissions
from ots.core.errors import NotFound, OtsError, ValidationFailed
from ots.core.idempotency import idempotency_exempt, idempotency_optional

from . import corrections, selectors, selfies, services, sessions
from .models import AttendanceStatus, WorkSession


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


# ================================================================ screens (M06.4, M06.5)


def _uuid(raw: object) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(raw))
    except (ValueError, TypeError):
        return None


def _person(request: HttpRequest, raw: object) -> User:
    """The person whose attendance is asked for, only if the viewer may see them (else it looks missing)."""
    pk = _uuid(raw)
    u = User.objects.visible_to(request.user).filter(pk=pk, is_break_glass=False).first() if pk else None
    if u is None:
        raise NotFound("Person not found.")
    return u


def _back(request: HttpRequest, user: User, day: datetime.date | None = None) -> HttpResponse:
    url = f"/app/attendance/{user.pk}"
    if day:
        url += f"?month={day:%Y-%m}&day={day.isoformat()}"
    return redirect(url)


@login_required
@require_http_methods(["GET"])
def my_attendance(request: HttpRequest) -> HttpResponse:
    pick = _uuid(request.GET.get("person"))
    if (
        pick is not None
        and User.objects.visible_to(request.user).filter(pk=pick, is_break_glass=False).exists()
    ):
        return redirect(f"/app/attendance/{pick}")
    return redirect(f"/app/attendance/{request.user.pk}")


@login_required
@require_http_methods(["GET"])
def attendance_page(request: HttpRequest, user_id: str) -> HttpResponse:
    person = _person(request, user_id)
    first = selectors.parse_month(request.GET.get("month"))
    day = selectors.parse_day(request.GET.get("day"))
    ctx = {
        "page_title": "My attendance" if person.pk == request.user.pk else f"Attendance: {person.full_name}",
        "person": person,
        "grid": selectors.month_grid(person, first),
        "people": selectors.people_for(request.user) if len(selectors.people_for(request.user)) > 1 else [],
        "day": day,
        "statuses": AttendanceStatus.choices,
        "can_correct": person.pk != request.user.pk and request.user.role != "EMPLOYEE",
        "can_force": request.user.role == "SUPER_ADMIN",
    }
    if day is not None:
        detail = selectors.day_detail(person, day)
        ctx["detail"] = detail
        ctx["can_see_selfie"] = request.user.role == "SUPER_ADMIN" or person.pk == request.user.pk
    return render(request, "attendance/calendar.html", ctx)


@login_required
@require_http_methods(["GET"])
def selfie_image(request: HttpRequest, session_id: str) -> HttpResponse:
    pk = _uuid(session_id)
    ws = WorkSession.objects.select_related("user").filter(pk=pk).first() if pk else None
    if ws is None:
        raise NotFound("Picture not found.")
    permissions.require(request.user, "selfie.view", ws)
    path = selfies.full_path(ws.selfie_path)
    if not path.is_file():
        raise NotFound("Picture not found.")
    with transaction.atomic():
        audit.record("selfie.viewed", request.user, ws.user, {"session": str(ws.pk)}, request)
    resp = FileResponse(path.open("rb"), content_type="image/jpeg")
    resp["Cache-Control"] = "private, no-store"
    return resp


def _day_from(raw: str) -> datetime.date:
    d = selectors.parse_day(raw)
    if d is None:
        raise ValidationFailed("Choose a date.", fields={"date": ["Invalid."]})
    return d


@login_required
@idempotency_optional
@require_http_methods(["POST"])
def correction_request(request: HttpRequest, user_id: str) -> HttpResponse:
    person = _person(request, user_id)
    day = selectors.parse_day(request.POST.get("date"))
    try:
        corrections.request_correction(
            request.user, person.pk, _day_from(request.POST.get("date", "")),
            request.POST.get("new_status", ""), request.POST.get("reason", ""),
        )  # fmt: skip
    except ValidationFailed as exc:
        return _error_page(request, exc)
    return _back(request, person, day)


def _error_page(request: HttpRequest, exc: OtsError) -> HttpResponse:
    return render(
        request, "attendance/error.html", {"page_title": "Check this", "message": exc.message}, status=422
    )


@login_required
@idempotency_optional
@require_http_methods(["POST"])
def correction_approve(request: HttpRequest, correction_id: str) -> HttpResponse:
    pk = _uuid(correction_id)
    if pk is None:
        raise NotFound("Not found.")
    corrections.approve_correction(request.user, pk)
    return redirect("/app/attendance/corrections")


@login_required
@require_http_methods(["GET"])
def corrections_page(request: HttpRequest) -> HttpResponse:
    permissions.require(request.user, "attendance.approve")
    return render(
        request, "attendance/corrections.html",
        {"page_title": "Attendance corrections", "items": selectors.pending_corrections()},
    )  # fmt: skip


@login_required
@idempotency_optional
@require_http_methods(["GET", "POST"])
def holidays_page(request: HttpRequest) -> HttpResponse:
    permissions.require(request.user, "holiday.manage")
    message = ""
    if request.method == "POST":
        try:
            if request.POST.get("remove", "").isdigit():
                corrections.remove_holiday(request.user, int(request.POST["remove"]))
            else:
                day = _day_from(request.POST.get("date", ""))
                corrections.add_holiday(request.user, day, request.POST.get("name", ""))
            return redirect("/app/attendance/holidays")
        except OtsError as exc:
            message = exc.message
    return render(
        request, "attendance/holidays.html",
        {"page_title": "Holidays", "items": selectors.holidays(), "message": message},
        status=422 if message else 200,
    )  # fmt: skip


@login_required
@idempotency_optional
@require_http_methods(["POST"])
def force_logout(request: HttpRequest, session_id: str) -> HttpResponse:
    pk = _uuid(session_id)
    if pk is None:
        raise NotFound("Not found.")
    try:
        ws = sessions.force_logout(request.user, pk, request.POST.get("reason", ""))
    except ValidationFailed as exc:
        return _error_page(request, exc)
    return _back(request, ws.user, ws.work_date)
