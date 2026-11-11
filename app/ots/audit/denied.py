"""security.denied: written on its own connection so it survives the rollback of the failed request."""

from __future__ import annotations

import threading
from typing import Any

from django.db import connections, transaction

from ots.core.errors import PermissionDenied

from . import api


def _write(request: Any, exc: PermissionDenied, actor_id: Any, out: list[BaseException]) -> None:
    try:
        with transaction.atomic():
            api.record(
                "security.denied",
                actor_id,
                None,
                {
                    "permission": exc.extra.get("action", ""),
                    "method": request.method,
                    "path": request.path[:200],
                },
                request,
            )
    except BaseException as e:  # noqa: BLE001
        out.append(e)
    finally:
        connections.close_all()  # this thread's connection only


def record_denied(request: Any, exc: PermissionDenied) -> None:
    user = getattr(request, "user", None)
    actor_id = user.pk if user is not None and user.is_authenticated else None
    errors: list[BaseException] = []
    t = threading.Thread(target=_write, args=(request, exc, actor_id, errors), name="audit-denied")
    t.start()
    t.join(timeout=10)
    if errors:
        raise errors[0]
