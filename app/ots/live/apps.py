from django.apps import AppConfig


class LiveConfig(AppConfig):
    name = "ots.live"
    label = "live"

    def ready(self) -> None:
        from . import publisher  # noqa: F401  (registers the event subscribers)
