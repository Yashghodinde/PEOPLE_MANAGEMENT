from django.apps import AppConfig


class AuditConfig(AppConfig):
    name = "ots.audit"
    label = "audit"

    def ready(self) -> None:
        from ots.core import middleware

        from . import denied

        middleware.denied_hook = denied.record_denied
