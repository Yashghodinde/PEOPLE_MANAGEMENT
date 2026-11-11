from __future__ import annotations

from django.conf import settings
from django.contrib.auth import login, logout
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_http_methods

from ots.core.errors import Locked, ValidationFailed
from ots.core.idempotency import idempotency_exempt

from . import auth, services

BACKEND = "django.contrib.auth.backends.ModelBackend"


def _next_url(request: HttpRequest) -> str:
    nxt = request.POST.get("next") or request.GET.get("next") or ""
    ok = url_has_allowed_host_and_scheme(
        nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    )
    return nxt if nxt and ok else settings.OTS_HOME_URL


@idempotency_exempt
@require_http_methods(["GET", "POST"])
def login_view(request: HttpRequest) -> HttpResponse:
    if request.user.is_authenticated:
        return redirect(settings.OTS_HOME_URL)
    ctx = {"next": request.GET.get("next", ""), "login_id": ""}
    if request.method == "POST":
        login_id, password = request.POST.get("login_id", ""), request.POST.get("password", "")
        ctx["login_id"] = login_id
        try:
            user = auth.check_password_login(login_id, password, request)
        except Locked as exc:
            ctx["error"] = exc.message
            return render(request, "accounts/login.html", ctx, status=423)
        except ValidationFailed as exc:
            ctx["error"] = exc.message
            return render(request, "accounts/login.html", ctx, status=400)
        if settings.OTS_SELFIE_STAGE_ENABLED:  # M06 turns this on: selfie comes before the session starts
            request.session["pending_user_id"] = str(user.pk)
            return redirect("/login/selfie")
        login(request, user, backend=BACKEND)
        return redirect("/password/change" if user.must_change_password else _next_url(request))
    return render(request, "accounts/login.html", ctx)


@require_http_methods(["POST"])
@idempotency_exempt
def logout_view(request: HttpRequest) -> HttpResponse:
    logout(request)
    return redirect("/login")


@require_http_methods(["GET", "POST"])
def change_password_view(request: HttpRequest) -> HttpResponse:
    if not request.user.is_authenticated:
        return redirect("/login")
    ctx: dict[str, object] = {"forced": request.user.must_change_password}
    if request.method == "POST":
        new, again = request.POST.get("new_password", ""), request.POST.get("new_password2", "")
        try:
            if new != again:
                raise ValidationFailed(
                    "The two new passwords are different.", fields={"new_password2": ["Different."]}
                )
            services.change_password(request.user, request.POST.get("old_password", ""), new, request=request)
        except ValidationFailed as exc:
            ctx["error"] = exc.message
            return render(request, "accounts/change_password.html", ctx, status=422)
        return redirect(settings.OTS_HOME_URL)
    return render(request, "accounts/change_password.html", ctx)
