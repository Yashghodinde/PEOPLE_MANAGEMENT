"""F-BK-4: the WAL spool is filling. WARNING at 6 GB, CRITICAL at 7.5 GB (sparse files, no real disk used)."""

from tests.backup.test_shipper import (  # noqa: F401
    _on_commit,
    dirs,
    handlers,
)
