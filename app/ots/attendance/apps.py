from django.apps import AppConfig


class AttendanceConfig(AppConfig):
    name = "ots.attendance"
    label = "attendance"

    def ready(self) -> None:
        from . import permissions, timeline  # noqa: F401  (policies and timeline providers)
