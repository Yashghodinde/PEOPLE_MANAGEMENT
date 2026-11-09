import uuid

from uuid6 import uuid7 as _uuid7


def uuid7() -> uuid.UUID:
    """Time-ordered UUID used for every primary key."""
    return uuid.UUID(bytes=_uuid7().bytes)
