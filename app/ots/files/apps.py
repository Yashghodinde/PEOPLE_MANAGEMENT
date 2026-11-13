from django.apps import AppConfig


class FilesConfig(AppConfig):
    name = "ots.files"
    label = "files"

    def ready(self) -> None:
        from . import handlers, permissions  # noqa: F401  (policies)

        handlers.register()  # the storage queue reports archived files here
