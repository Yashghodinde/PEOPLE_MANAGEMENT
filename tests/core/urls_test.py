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
