from django.apps import AppConfig


class TasksConfig(AppConfig):
    name = "ots.tasks"
    label = "tasks"

    def ready(self) -> None:
        from . import permissions  # noqa: F401  (registers the task policies)
