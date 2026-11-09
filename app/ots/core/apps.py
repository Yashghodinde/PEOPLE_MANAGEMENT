from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = "ots.core"
    label = "ots_core"

    def ready(self) -> None:
        # Connect the failure hooks of the dispatcher and scheduler to the alerts API.
        from . import alerts, dispatcher, jobs

        dispatcher.alert_hook = alerts.raise_alert
        jobs.alert_hook = alerts.raise_alert
        jobs.resolve_hook = alerts.resolve_alert
