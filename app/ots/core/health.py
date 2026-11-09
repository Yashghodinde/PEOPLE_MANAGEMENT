"""Minimal /healthz: DB connectivity and free disk on OTS_DATA_DIR (extended in M00.8)."""

import shutil

from django.conf import settings
from django.db import connection
from django.http import HttpRequest, JsonResponse

MIN_FREE_FRACTION = 0.10


def check_db() -> str:
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
    except Exception:
        return "error"
    return "ok"


def check_disk() -> str:
    try:
        usage = shutil.disk_usage(settings.OTS_DATA_DIR)
    except OSError:
        return "error"
    return "ok" if usage.free / usage.total > MIN_FREE_FRACTION else "critical"


def healthz(request: HttpRequest) -> JsonResponse:
    body = {"db": check_db(), "disk": check_disk()}
    status = 200 if all(v == "ok" for v in body.values()) else 503
    return JsonResponse(body, status=status)
