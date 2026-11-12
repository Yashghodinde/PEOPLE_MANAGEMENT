"""audit.record(): the only writer of audit_log. Must run inside the business transaction."""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.db import connection

from ots.core.clock import now
from ots.core.logging import request_id_var

from .models import AuditLog


def _client_ip(request: Any) -> str | None:
    """The caller's address. X-Forwarded-For is trusted only as far as OTS_TRUSTED_PROXY_HOPS says:
    with N proxies in front, the real client is the Nth entry from the right (the one our own proxy
    added). Entries further left were supplied by the client and are ignored."""
    if request is None:
        return None
    hops = getattr(settings, "OTS_TRUSTED_PROXY_HOPS", 0)
    xff: str = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if hops > 0 and xff:
        parts = [p.strip() for p in xff.split(",") if p.strip()]
        if len(parts) >= hops:
            return parts[-hops]
    remote: str = request.META.get("REMOTE_ADDR", "")
    return remote or None


def _target(target: Any) -> tuple[str, str]:
    if target is None:
        return "", ""
    if isinstance(target, tuple):
        return str(target[0]), str(target[1])
    return target._meta.model_name, str(target.pk)


def record(
    action: str,
    actor: Any = None,
    target: Any = None,
    payload: dict[str, Any] | None = None,
    request: Any = None,
) -> AuditLog:
    if not connection.in_atomic_block:
        raise RuntimeError("audit.record() must be called inside transaction.atomic()")
    target_type, target_id = _target(target)
    rid = getattr(request, "request_id", None) or request_id_var.get()
    return AuditLog.objects.create(
        at=now(),
        actor_id=getattr(actor, "pk", actor),
        action=action,
        target_type=target_type,
        target_id=target_id,
        payload=payload or {},
        client_ip=_client_ip(request),
        request_id="" if rid == "-" else rid,
    )
