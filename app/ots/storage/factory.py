"""Pick the storage backend from settings (OTS_STORAGE_BACKEND = graph | fake)."""

from __future__ import annotations

from django.conf import settings

from .errors import ConfigurationError
from .provider import StorageProvider

_provider: StorageProvider | None = None  # created on first use


def get_provider() -> StorageProvider:
    global _provider  # noqa: PLW0603
    if _provider is None:
        backend = settings.OTS_STORAGE_BACKEND
        if backend == "fake":
            from .fake import LocalDirProvider

            _provider = LocalDirProvider(settings.OTS_STORAGE_FAKE_DIR)
        elif backend == "graph":
            from .graph import GraphProvider  # added in M05.2

            _provider = GraphProvider.from_settings()  # type: ignore[assignment]  # real provider: M05.2
        else:
            raise ConfigurationError(f"OTS_STORAGE_BACKEND must be 'graph' or 'fake', not {backend!r}")
    return _provider  # type: ignore[return-value]  # set by the branch above


def reset_provider() -> None:
    """For tests: forget the cached provider."""
    global _provider  # noqa: PLW0603
    _provider = None
