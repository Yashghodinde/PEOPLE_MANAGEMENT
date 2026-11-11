"""Role-specific sidebar navigation. Items whose pages do not exist yet point to '#'."""

from dataclasses import dataclass


@dataclass(frozen=True)
class NavItem:
    label: str
    icon: str
    path: str
    roles: tuple[str, ...] = ("SUPER_ADMIN", "SUB_ADMIN", "EMPLOYEE")


ITEMS = [
    NavItem("Dashboard", "layout-dashboard", "/shells/dashboard"),
    NavItem("Tasks", "list-checks", "/shells/list-detail"),
    NavItem("Files", "folder", "#"),
    NavItem("Attendance", "calendar", "#"),
    NavItem("People", "users", "/app/people", ("SUPER_ADMIN", "SUB_ADMIN")),
    NavItem("Org chart", "user", "/app/org", ("SUPER_ADMIN", "SUB_ADMIN")),
    NavItem("Reports", "activity", "#", ("SUPER_ADMIN", "SUB_ADMIN")),
    NavItem("System health", "hard-drive", "#", ("SUPER_ADMIN",)),
    NavItem("Settings", "settings", "/shells/settings", ("SUPER_ADMIN",)),
]


def items_for(role: str | None) -> list[NavItem]:
    # Until accounts exist (M02) the shells show the Super Admin navigation.
    r = role or "SUPER_ADMIN"
    return [i for i in ITEMS if r in i.roles]
