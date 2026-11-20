"""Force the change-password page while must_change_password is set."""

from __future__ import annotations

import re
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


# What an HR account may never open: everything about tasks, and every part of Files except their own personal
# storage. The scoped queries already return nothing for HR; this fence is a second lock, so a guessed address
# or a screen added later cannot open the door by accident.
HR_BLOCKED_PREFIXES = (
    "/app/tasks",
    "/app/assignments",
    "/app/day-close",
    "/app/day-resume",
    "/app/receipts",
    "/files",
)
# The only /files addresses HR may use: My storage and its folders, uploading, and opening or renaming or
# hiding their own files (the file queries still limit those to files HR owns).
HR_FILE_ALLOWED = re.compile(
    r"^/files/(mine(/|$)|uploads(/|$)|check-name$"
    r"|v/[0-9a-f-]{36}/(download|preview)$"
    r"|[0-9a-f-]{36}/(rename|hide)$)"
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
            and not HR_FILE_ALLOWED.match(request.path)
        ):
            from ots.core.errors import PermissionDenied
            from ots.core.middleware import ErrorMappingMiddleware

            denied = PermissionDenied("HR has no access to tasks or to other files.", action="hr.fence")
            mapper = ErrorMappingMiddleware(self.get_response)
            return mapper.process_exception(request, denied)  # type: ignore[return-value]
        return self.get_response(request)
