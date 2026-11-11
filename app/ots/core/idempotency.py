"""Idempotency layer (F-CORE-1).

Flow for POST/PUT/PATCH/DELETE under /app/ and /api/:
  1. Key must be a UUID (428 if missing, 400 if malformed) unless the view is @idempotency_exempt.
  2. Inside one transaction, INSERT the key with ON CONFLICT DO NOTHING. A concurrent duplicate blocks on the
     unique index until the first request commits, then replays its stored response.
  3. The view runs in a savepoint: a 4xx/5xx rolls its writes back; 5xx also removes the key so the client
     may retry; success and 4xx are stored.
"""

import gzip
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any, TypeVar

from django.db import connection, transaction
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.urls import Resolver404, resolve

from .clock import now
from .models import IdempotencyRecord

F = TypeVar("F", bound=Callable[..., Any])  # noqa: UP047
HEADER = "Idempotency-Key"
METHODS = {"POST", "PUT", "PATCH", "DELETE"}
PREFIXES = ("/app/", "/api/")
RETENTION_DAYS = 7


def idempotency_exempt(view: F) -> F:  # noqa: UP047
    view.idempotency_exempt = True  # type: ignore[attr-defined]
    return view


def idempotency_optional(view: F) -> F:  # noqa: UP047
    """Plain HTML form posts (no JavaScript) carry no key. A key is not required, but when one is sent it is
    enforced and replayed as usual."""
    view.idempotency_optional = True  # type: ignore[attr-defined]
    return view


def _is_optional(path: str) -> bool:
    try:
        match = resolve(path)
    except Resolver404:
        return False
    return bool(getattr(match.func, "idempotency_optional", False))


def _is_exempt(path: str) -> bool:
    try:
        match = resolve(path)
    except Resolver404:
        return False
    return bool(getattr(match.func, "idempotency_exempt", False))


class _Rollback(Exception):
    """Internal: unwinds a savepoint without propagating."""


class IdempotencyMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if request.method not in METHODS or not request.path.startswith(PREFIXES):
            return self.get_response(request)
        if _is_exempt(request.path_info):
            return self.get_response(request)

        raw = request.headers.get(HEADER)
        if not raw and _is_optional(request.path_info):
            return self.get_response(request)
        if not raw:
            return _error(428, "idempotency_key_required", f"{HEADER} header is required")
        try:
            key = uuid.UUID(raw)
        except ValueError:
            return _error(400, "idempotency_key_invalid", f"{HEADER} must be a UUID")

        user = getattr(request, "user", None)
        user_id = user.pk if user is not None and user.is_authenticated else None
        return self._handle(request, key, user_id)

    def _handle(self, request: HttpRequest, key: uuid.UUID, user_id: Any) -> HttpResponse:
        method = request.method or ""
        with transaction.atomic():
            claimed = _claim(key, user_id, method, request.path)
            if not claimed:
                return _replay(key, user_id, request)

            holder: list[HttpResponse] = []
            try:
                with transaction.atomic():  # savepoint around the view
                    holder.append(self.get_response(request))
                    if holder[0].status_code >= 400 or holder[0].streaming:
                        raise _Rollback
            except _Rollback:
                pass
            response = holder[0]

            if response.status_code >= 500 or response.streaming:
                transaction.set_rollback(True)  # drop the claim; client may retry
                return response
            _store(key, response)
            return response


def _error(status: int, code: str, message: str) -> JsonResponse:
    return JsonResponse({"error": code, "message": message}, status=status)


def _claim(key: uuid.UUID, user_id: Any, method: str, path: str) -> bool:
    with connection.cursor() as cur:
        cur.execute(
            "INSERT INTO idempotency_records (key, user_id, method, path, status_code, content_type,"
            " response_body, created_at) VALUES (%s, %s, %s, %s, 0, '', ''::bytea, now())"
            " ON CONFLICT (key) DO NOTHING",
            [key, user_id, method, path],
        )
        return bool(cur.rowcount == 1)


def _store(key: uuid.UUID, response: HttpResponse) -> None:
    IdempotencyRecord.objects.filter(key=key).update(
        status_code=response.status_code,
        content_type=response.get("Content-Type", "application/octet-stream"),
        response_body=gzip.compress(response.content),
    )


def _replay(key: uuid.UUID, user_id: Any, request: HttpRequest) -> HttpResponse:
    rec = IdempotencyRecord.objects.select_for_update().get(key=key)
    if rec.user_id != user_id or rec.method != request.method or rec.path != request.path:
        return _error(422, "idempotency_key_reused", "This key was used for a different request")
    response = HttpResponse(
        gzip.decompress(bytes(rec.response_body)), status=rec.status_code, content_type=rec.content_type
    )
    response["Idempotent-Replay"] = "true"
    return response


def purge_older_than_days(days: int = RETENTION_DAYS) -> int:
    deleted, _ = IdempotencyRecord.objects.filter(created_at__lt=now() - timedelta(days=days)).delete()
    return deleted
