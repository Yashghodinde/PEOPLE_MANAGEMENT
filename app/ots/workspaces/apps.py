from django.apps import AppConfig


class WorkspacesConfig(AppConfig):
    name = "ots.workspaces"
    label = "workspaces"

    def ready(self) -> None:
        from . import permissions  # noqa: F401  (registers the File ID policies)
