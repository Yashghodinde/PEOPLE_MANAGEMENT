"""Pairing and revoking office TVs (M10 section 3). Every action is audited; the token is never stored."""

from __future__ import annotations

import datetime
import hashlib
import secrets
from typing import Any

from django.core.cache import cache
from django.db import transaction
from django.http import HttpRequest

from ots.audit import api as audit
from ots.core.clock import now
from ots.core.errors import OtsError, ValidationFailed
from ots.core.permissions import policy, require

from .models import DisplayDevice, DisplayPairingCode

CODE_MINUTES = 10
COOKIE = "ots_display"
COOKIE_MAX_AGE = 365 * 24 * 3600
MAX_TRIES = 10  # wrong codes per address per window
TRY_WINDOW = 600
SEEN_EVERY = 60  # last_seen_at is written at most once a minute


class TooManyAttempts(OtsError):
    status = 429
    code = "too_many_attempts"


@policy("display.manage")
def can_manage(user: Any, obj: Any = None) -> bool:
    return bool(user and getattr(user, "is_active", False) and user.role == "SUPER_ADMIN")


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _ip(request: HttpRequest | None) -> str:
    return str(request.META.get("REMOTE_ADDR", "")) if request is not None else ""


def create_code(user: Any, name: str, request: HttpRequest | None = None) -> tuple[str, DisplayPairingCode]:
    """Make a fresh 6-digit code for a new TV. The plain code is returned once and never kept."""
    require(user, "display.manage")
    name = " ".join((name or "").split())
    if not name or len(name) > 80:
        raise ValidationFailed(
            "Give the display a name of up to 80 characters.", fields={"name": ["Invalid."]}
        )
    at = now()
    for _ in range(20):
        code = f"{secrets.randbelow(10**6):06d}"
        h = _hash(code)
        if not DisplayPairingCode.objects.filter(
            code_hash=h, used_at__isnull=True, expires_at__gt=at
        ).exists():
            break
    else:  # pragma: no cover  (a million codes, a handful live at once)
        raise ValidationFailed("Try again in a moment.")
    with transaction.atomic():
        row = DisplayPairingCode.objects.create(
            code_hash=h,
            name=name,
            created_by=user,
            created_at=at,
            expires_at=at + datetime.timedelta(minutes=CODE_MINUTES),
        )
        audit.record("display.code_created", user, row, {"name": name}, request)
    return code, row


def _failed(request: HttpRequest | None) -> None:
    key = f"display-pair-fail:{_ip(request)}"
    cache.add(key, 0, TRY_WINDOW)
    try:
        cache.incr(key)
    except ValueError:  # expired between add and incr
        cache.set(key, 1, TRY_WINDOW)


def pair(code: str, request: HttpRequest | None = None) -> tuple[DisplayDevice, str]:
    """The TV sends its code. Returns the new device and its token (goes in a cookie; it is not stored)."""
    if int(cache.get(f"display-pair-fail:{_ip(request)}", 0)) >= MAX_TRIES:
        raise TooManyAttempts("Too many wrong codes. Wait ten minutes and ask for a new code.")
    code = "".join(ch for ch in (code or "") if ch.isdigit())
    at = now()
    with transaction.atomic():
        row = (
            DisplayPairingCode.objects.select_for_update()
            .filter(code_hash=_hash(code), used_at__isnull=True, expires_at__gt=at)
            .first()
            if len(code) == 6
            else None
        )
        if row is None:
            _failed(request)
            raise ValidationFailed(
                "That code is not valid, or it has expired.", fields={"code": ["Invalid."]}
            )
        token = secrets.token_urlsafe(32)  # 256 bits
        row.used_at = at
        row.save(update_fields=["used_at"])
        device = DisplayDevice.objects.create(
            name=row.name, token_hash=_hash(token), created_by=row.created_by, created_at=at, last_seen_at=at
        )
        audit.record(
            "display.paired", row.created_by, device, {"name": device.name, "ip": _ip(request)}, request
        )
    return device, token


def authenticate(token: str | None) -> DisplayDevice | None:
    """The device this cookie belongs to, if it is still allowed (not revoked)."""
    if not token or len(token) > 100:
        return None
    device = DisplayDevice.objects.filter(token_hash=_hash(token), revoked_at__isnull=True).first()
    if device is None:
        return None
    at = now()
    if device.last_seen_at is None or (at - device.last_seen_at).total_seconds() > SEEN_EVERY:
        DisplayDevice.objects.filter(pk=device.pk).update(last_seen_at=at)
    return device


def revoke(user: Any, device_id: Any, request: HttpRequest | None = None) -> DisplayDevice:
    """Cut a TV off. Its open page is told to reload, which lands on the pairing screen."""
    require(user, "display.manage")
    with transaction.atomic():
        device = DisplayDevice.objects.select_for_update().filter(pk=device_id).first()
        if device is None:
            from ots.core.errors import NotFound

            raise NotFound("No such display.")
        if device.revoked_at is None:
            device.revoked_at, device.revoked_by = now(), user
            device.save(update_fields=["revoked_at", "revoked_by"])
            audit.record("display.revoked", user, device, {"name": device.name}, request)
            from .publisher import send

            send(users=[f"display:{device.pk}"], type_="reload")
    return device


def devices() -> list[DisplayDevice]:
    return list(DisplayDevice.objects.order_by("revoked_at", "-created_at")[:100])
