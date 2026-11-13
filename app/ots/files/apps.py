from django.apps import AppConfig


class FilesConfig(AppConfig):
    name = "ots.files"
    label = "files"

    def ready(self) -> None:
        from . import permissions  # noqa: F401  (policies)
