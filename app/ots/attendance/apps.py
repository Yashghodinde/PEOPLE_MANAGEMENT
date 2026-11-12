from django.apps import AppConfig


class AttendanceConfig(AppConfig):
    name = "ots.attendance"
    label = "attendance"

    def ready(self) -> None:
        from . import permissions  # noqa: F401  (registers the policies)
