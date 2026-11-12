"""Microsoft Graph provider. Placeholder until M05.2 fills it in."""

from __future__ import annotations

from .errors import ConfigurationError


class GraphProvider:
    @classmethod
    def from_settings(cls) -> GraphProvider:
        raise ConfigurationError("GraphProvider is not built yet (M05.2); use OTS_STORAGE_BACKEND=fake")
