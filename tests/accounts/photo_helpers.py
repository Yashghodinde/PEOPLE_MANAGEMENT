"""Build pictures for the reference-photo tests."""

from __future__ import annotations

import io

from PIL import Image


def picture(
    size: tuple[int, int] = (1200, 800), fmt: str = "JPEG", *, orientation: int | None = None, mode: str = "RGB",
    colors: tuple[tuple[int, int, int], ...] = ((255, 0, 0), (0, 200, 0), (0, 0, 255)),
) -> bytes:  # fmt: skip
    """Three vertical bands (red, green, blue) so crops and rotations can be checked by looking at pixels."""
    w, h = size
    img = Image.new("RGB", size)
    for i, color in enumerate(colors):
        img.paste(color, (w * i // 3, 0, w * (i + 1) // 3, h))
    if mode != "RGB":
        img = img.convert(mode)
    out = io.BytesIO()
    kwargs: dict = {}
    if orientation is not None and fmt == "JPEG":
        exif = Image.Exif()
        exif[0x0112] = orientation  # Orientation
        exif[0x013B] = "secret artist name"  # Artist: personal metadata that must not survive
        exif[0x010F] = "SecretCam"  # Make
        kwargs["exif"] = exif
    img.save(out, fmt, **kwargs)
    return out.getvalue()


def open_stored(data: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(data))
    img.load()
    return img
