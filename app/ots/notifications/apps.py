from django.apps import AppConfig


class NotificationsConfig(AppConfig):
    name = "ots.notifications"
    label = "notifications"

    def ready(self) -> None:
        from . import subscribers  # noqa: F401  (registers the event subscribers)
