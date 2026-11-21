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


# What an HR account may never open: everything about tasks. HR has no tasks, so the scoped queries already
# return nothing for HR; this fence is a second lock, so a guessed address or a screen added later cannot open
# the door by accident. Files are open to HR on the same terms as an Employee.
HR_BLOCKED_PREFIXES = (
    "/app/tasks",
    "/app/assignments",
    "/app/day-close",
    "/app/day-resume",
    "/app/receipts",
)


class HrFenceMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        user = getattr(request, "user", None)
        if (
            user is not None
            and user.is_authenticated
            and getattr(user, "role", "") == "HR"
            and request.path.startswith(HR_BLOCKED_PREFIXES)
        ):
            from ots.core.errors import PermissionDenied
            from ots.core.middleware import ErrorMappingMiddleware

            denied = PermissionDenied("HR has no access to tasks.", action="hr.fence")
            mapper = ErrorMappingMiddleware(self.get_response)
            return mapper.process_exception(request, denied)  # type: ignore[return-value]
        return self.get_response(request)
