"""/healthz (no auth; Caddy restricts it to the LAN): db, disk and worker liveness.

Worker liveness is read from the database (the watchdog job runs every minute) because the app container
cannot see the worker container's heartbeat file.
"""

import shutil

from django.conf import settings
from django.db import connection
from django.http import HttpRequest, JsonResponse

from .clock import now

MIN_FREE_FRACTION = 0.10
WORKER_STALE_SECONDS = 300


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


def worker_heartbeat_age_s() -> int | None:
    """Seconds since the watchdog job last finished; None if it has never run (fresh install)."""
    from .models import Job

    last = Job.objects.filter(name="jobs.watchdog").values_list("last_finished_at", flat=True).first()
    return None if last is None else int((now() - last).total_seconds())


def healthz(request: HttpRequest) -> JsonResponse:
    db = check_db()
    age = worker_heartbeat_age_s() if db == "ok" else None
    body: dict[str, object] = {"db": db, "disk": check_disk(), "worker_heartbeat_age_s": age}
    worker_bad = age is not None and age > WORKER_STALE_SECONDS
    critical = db != "ok" or body["disk"] != "ok" or worker_bad
    return JsonResponse(body, status=503 if critical else 200)
