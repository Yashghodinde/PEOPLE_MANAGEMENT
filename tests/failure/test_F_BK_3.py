"""F-BK-3: WAL is not shipping. Amber at 10 minutes, red at 15; after recovery everything ships in order."""

from tests.backup.test_shipper import (  # noqa: F401
    _on_commit,
    dirs,
    handlers,
    remote,
)
