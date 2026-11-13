from __future__ import annotations

from typing import Any

from ots.core.errors import OtsError


class OffsetMismatch(OtsError):
    """A piece does not start where the server copy ends (F-FL-1). The client resumes at `received_bytes`."""

    status = 416
    code = "offset_mismatch"

    def __init__(self, received_bytes: int, **extra: Any) -> None:
        super().__init__("Resume from the server's offset.", received_bytes=received_bytes, **extra)


class HashMismatch(OtsError):
    """The finished upload does not match the SHA-256 the browser promised (F-FL-2). Nothing was kept."""

    status = 422
    code = "hash_mismatch"
