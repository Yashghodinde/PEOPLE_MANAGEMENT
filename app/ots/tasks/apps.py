from django.apps import AppConfig


class TasksConfig(AppConfig):
    name = "ots.tasks"
    label = "tasks"

    def ready(self) -> None:
        from ots.accounts import guards

        from . import permissions, selectors  # noqa: F401  (permissions registers the task policies)

        guards.register(selectors.open_assignment_lines)  # a person with open work cannot be deactivated
