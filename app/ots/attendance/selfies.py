"""Attendance selfies (M06 section 1). Checked, shrunk and re-saved without metadata; stored by date."""

from __future__ import annotations

import io
import logging
import os
from pathlib import Path
from uuid import UUID

from django.conf import settings
from PIL import Image, ImageOps, UnidentifiedImageError

from ots.core.errors import StorageUnavailable, ValidationFailed

log = logging.getLogger(__name__)

MAX_BYTES = 200 * 1024
WIDTH, HEIGHT, TOLERANCE = 480, 360, 0.10
QUALITY = 70


def _bad(message: str) -> ValidationFailed:
    return ValidationFailed(message, fields={"selfie": [message]})


def process(data: bytes) -> bytes:
    """A JPEG of about 480 x 360, at most 200 KB. Returns the stored form (EXIF removed)."""
    if not data:
        raise _bad("The picture did not arrive. Try again.")
    if len(data) > MAX_BYTES:
        raise _bad("The picture is too large.")
    if data[:3] != b"\xff\xd8\xff":
        raise _bad("The picture must be a JPEG.")
    try:
        image = Image.open(io.BytesIO(data))
        if image.format != "JPEG":
            raise _bad("The picture must be a JPEG.")
        w, h = image.size
        if abs(w - WIDTH) > WIDTH * TOLERANCE or abs(h - HEIGHT) > HEIGHT * TOLERANCE:
            raise _bad("The picture has the wrong size.")
        image.load()
    except ValidationFailed:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError):
        raise _bad("That is not a usable picture.") from None
    image = ImageOps.exif_transpose(image).convert("RGB")
    out = io.BytesIO()
    image.save(out, "JPEG", quality=QUALITY, optimize=True)  # no exif= argument: nothing is carried over
    return out.getvalue()


def relative_path(session_id: UUID, day: object) -> str:
    return f"{day:%Y/%m/%d}/{UUID(str(session_id))}.jpg"  # type: ignore[str-format]


def full_path(relative: str) -> Path:
    return Path(settings.OTS_SELFIE_DIR) / relative


def stage(relative: str, jpeg: bytes) -> Path:
    """Before the commit: write beside the final name. A failure here fails the login cleanly (F-AT-7)."""
    new = full_path(relative).with_suffix(".jpg.new")
    try:
        new.parent.mkdir(parents=True, exist_ok=True)
        new.write_bytes(jpeg)
        new.chmod(0o640)
    except OSError as exc:
        log.exception("could not stage a selfie", extra={"event": "selfie.stage_failed"})
        raise StorageUnavailable(
            "The picture could not be saved. Try again, or tell the administrator."
        ) from exc
    return new


def publish(relative: str) -> None:
    final = full_path(relative)
    try:
        os.replace(final.with_suffix(".jpg.new"), final)
    except OSError:
        log.exception("could not publish a selfie", extra={"event": "selfie.publish_failed"})


def discard(relative: str) -> None:
    try:
        full_path(relative).with_suffix(".jpg.new").unlink(missing_ok=True)
    except OSError:
        log.exception("could not remove a staged selfie", extra={"event": "selfie.discard_failed"})
