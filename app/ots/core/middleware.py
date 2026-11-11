import logging
import re
from collections.abc import Callable

from django.http import HttpRequest, HttpResponse, JsonResponse

from .errors import OtsError, PermissionDenied
from .ids import uuid7
from .logging import request_id_var, user_id_var

_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class RequestIdMiddleware:
    """Accept or generate X-Request-Id, expose it to logging, echo it back.

    Place after AuthenticationMiddleware so user_id is available to log records.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        incoming = request.headers.get("X-Request-Id", "")
        rid = incoming if _SAFE_ID.match(incoming) else str(uuid7())
        user = getattr(request, "user", None)
        uid = str(user.pk) if user is not None and user.is_authenticated else "-"
        t1, t2 = request_id_var.set(rid), user_id_var.set(uid)
        request.request_id = rid  # type: ignore[attr-defined]
        try:
            response = self.get_response(request)
        finally:
            request_id_var.reset(t1)
            user_id_var.reset(t2)
        response["X-Request-Id"] = rid
        return response


def wants_json(request: HttpRequest) -> bool:
    return (
        request.headers.get("HX-Request") == "true"
        or "application/json" in request.headers.get("Accept", "")
        or request.path.startswith("/api/")
    )


# Set by ots.audit at startup: writes security.denied. The view's savepoint is rolled back on a 403,
# so the hook must write on its own connection (see ots.audit.denied).
denied_hook: Callable[[HttpRequest, PermissionDenied], None] | None = None


class ErrorMappingMiddleware:
    """Map OtsError to a response. Never exposes stack traces or exception internals."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        return self.get_response(request)

    def process_exception(self, request: HttpRequest, exception: Exception) -> HttpResponse | None:
        if not isinstance(exception, OtsError):
            return None
        if isinstance(exception, PermissionDenied) and denied_hook is not None:
            try:
                denied_hook(request, exception)
            except Exception:  # noqa: BLE001  (auditing must never turn a 403 into a 500)
                logging.getLogger(__name__).exception("could not record security.denied")
        if wants_json(request):
            return JsonResponse(exception.to_dict(), status=exception.status)
        from django.template.loader import render_to_string

        html = render_to_string(
            "errors/error.html",
            {
                "status": exception.status,
                "message": exception.message,
                "request_id": getattr(request, "request_id", "-"),
            },
        )
        return HttpResponse(html, status=exception.status)
