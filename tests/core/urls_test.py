from django.http import HttpResponse
from django.urls import path
from ots.core import errors
from ots.core.logging import request_id_var


def make(exc):
    def view(request):
        raise exc

    return view


def rid(request):
    return HttpResponse(request_id_var.get())


urlpatterns = [
    path("rid", rid),
    path("e/403", make(errors.PermissionDenied("nope"))),
    path("e/404", make(errors.NotFound("missing"))),
    path("e/409", make(errors.Conflict("stale", current={"v": 3}))),
    path("e/409t", make(errors.InvalidTransition("bad move"))),
    path("e/422", make(errors.ValidationFailed("invalid", fields={"title": "required"}))),
    path("e/423", make(errors.Locked("locked"))),
    path("e/503", make(errors.StorageUnavailable("down"))),
    path("e/507", make(errors.QuotaBlocked("full"))),
    path("e/xss", make(errors.NotFound("<script>x</script>"))),
]


# --- idempotency test views -------------------------------------------------
import time  # noqa: E402

from django.db import connection  # noqa: E402
from django.http import JsonResponse  # noqa: E402
from ots.core.idempotency import idempotency_exempt  # noqa: E402


def api_create(request):
    time.sleep(0.3)  # widen the race window for the concurrency test
    with connection.cursor() as cur:
        cur.execute("INSERT INTO idem_test (note) VALUES ('x') RETURNING id")
        new_id = cur.fetchone()[0]
    return JsonResponse({"id": new_id})


def api_4xx(request):
    with connection.cursor() as cur:
        cur.execute("INSERT INTO idem_test (note) VALUES ('should-roll-back')")
    return JsonResponse({"error": "nope"}, status=409)


def api_5xx(request):
    with connection.cursor() as cur:
        cur.execute("INSERT INTO idem_test (note) VALUES ('should-roll-back')")
    return JsonResponse({"error": "boom"}, status=500)


@idempotency_exempt
def api_exempt(request):
    return JsonResponse({"ok": True})


urlpatterns += [
    path("api/create", api_create),
    path("api/other", api_create),
    path("api/conflict", api_4xx),
    path("api/boom", api_5xx),
    path("api/exempt", api_exempt),
    path("plain/post", api_exempt),
]
