"""Errors raised by storage providers. The queue's `classify()` turns them into actions (M05.3)."""

from __future__ import annotations


class StorageProviderError(Exception):
    """Base for everything a provider raises on purpose."""


class GraphError(StorageProviderError):
    """An HTTP error answer from Microsoft Graph (or the fake provider imitating one)."""

    def __init__(
        self,
        status: int,
        code: str = "",
        body: str = "",
        retry_after: float | None = None,
        url_kind: str = "",
    ) -> None:
        super().__init__(f"graph {status} {code}".strip())
        self.status = status
        self.code = code  # Graph's error.code, e.g. "nameAlreadyExists"
        self.body = body[:2048]  # kept for alerts; never more than 2 KB
        self.retry_after = retry_after  # seconds, from the Retry-After header
        self.url_kind = url_kind  # "upload" when the failing URL was a pre-authenticated uploadUrl


class TransportFailure(StorageProviderError):
    """No usable answer: timeout, connection refused, DNS failure, TLS error."""


class VerificationFailed(StorageProviderError):
    """The remote copy does not match the local file (size or hash)."""


class WrongLibrary(StorageProviderError):
    """An operation was attempted on a library that does not allow it (e.g. deleting a business file)."""


class ConfigurationError(StorageProviderError):
    """Settings are missing or unusable (no certificate, unknown backend ...)."""
