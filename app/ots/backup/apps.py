from django.apps import AppConfig


class BackupConfig(AppConfig):
    name = "ots.backup"
    label = "backup"

    def ready(self) -> None:
        from . import handlers

        handlers.register()  # the storage queue reports shipped WAL and uploaded backups here
