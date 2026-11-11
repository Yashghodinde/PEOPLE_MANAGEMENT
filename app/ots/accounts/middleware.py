"""Force the change-password page while must_change_password is set."""

from __future__ import annotations

from collections.abc import Callable

from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect

ALLOWED_PREFIXES = ("/password/change", "/logout", "/healthz", "/static/", "/ca.crt")


class MustChangePasswordMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        user = getattr(request, "user", None)
        if (
            user is not None
            and user.is_authenticated
            and getattr(user, "must_change_password", False)
            and not request.path.startswith(ALLOWED_PREFIXES)
        ):
            return redirect("/password/change")
        return self.get_response(request)
