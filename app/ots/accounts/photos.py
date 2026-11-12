"""Reference photos (M02 rule 14): one small square JPEG per person, used later to compare attendance selfies.

An upload is never stored as it came in. It is checked (JPEG or PNG, at most 2 MB, a sane pixel count),
turned upright, cropped to a centred square, shrunk to 480 x 480 and saved again as a JPEG at quality 70
with no metadata. That is about 25 to 45 KB however large the original was, and it removes the camera's
location and device information.
"""

from __future__ import annotations

import io
import os
from pathlib import Path
from uuid import UUID

from django.conf import settings
from PIL import Image, ImageOps, UnidentifiedImageError

from ots.core.errors import ValidationFailed

MAX_UPLOAD_BYTES = 2 * 1024 * 1024
MAX_PIXELS = 40_000_000  # refuse absurd dimensions before decoding anything (decompression-bomb guard)
SIZE = 480
QUALITY = 70
ALLOWED = ("JPEG", "PNG")

Image.MAX_IMAGE_PIXELS = MAX_PIXELS


def _bad(message: str) -> ValidationFailed:
    return ValidationFailed(message, fields={"photo": [message]})


def process(data: bytes) -> bytes:
    """Validate an uploaded picture and return the stored form: a 480 x 480 JPEG."""
    if not data:
        raise _bad("Choose a picture first.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise _bad("The photo must be 2 MB or smaller.")
    try:
        image = Image.open(io.BytesIO(data))
        if image.format not in ALLOWED:
            raise _bad("Use a JPEG or PNG picture.")
        if image.width * image.height > MAX_PIXELS:  # read from the header, nothing is decoded yet
            raise _bad("That picture has too many pixels. Use a smaller one.")
        image.load()
    except ValidationFailed:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError):
        raise _bad("That file is not a usable JPEG or PNG picture.") from None
    image = ImageOps.exif_transpose(image)  # honour the camera's rotation, then forget it
    if image.mode in ("RGBA", "LA", "P"):  # transparency becomes white, not black
        rgba = image.convert("RGBA")
        flat = Image.new("RGB", rgba.size, (255, 255, 255))
        flat.paste(rgba, mask=rgba.getchannel("A"))
        image = flat
    else:
        image = image.convert("RGB")
    side = min(image.size)
    left, top = (image.width - side) // 2, (image.height - side) // 2
    image = image.crop((left, top, left + side, top + side)).resize((SIZE, SIZE), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    image.save(out, "JPEG", quality=QUALITY, optimize=True)  # no exif= argument: metadata is not carried over
    return out.getvalue()


def path_for(user_id: UUID | str) -> Path:
    """Where a person's photo lives. Built from the id only, never from anything a user typed."""
    return Path(settings.OTS_REFERENCE_PHOTO_DIR) / f"{UUID(str(user_id))}.jpg"


def write(user_id: UUID | str, jpeg: bytes) -> None:
    """Replace the file atomically: a reader sees the old picture or the new one, never half of one."""
    final = path_for(user_id)
    final.parent.mkdir(parents=True, exist_ok=True)
    tmp = final.with_suffix(".jpg.tmp")
    tmp.write_bytes(jpeg)
    tmp.chmod(0o640)
    os.replace(tmp, final)


def delete(user_id: UUID | str) -> None:
    path_for(user_id).unlink(missing_ok=True)
