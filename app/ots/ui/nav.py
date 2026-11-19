"""Role-specific sidebar navigation. A page that does not exist yet is not listed (a dead link is worse).

Reports (M11) and System health (M12) join the list when their pages are built."""

from dataclasses import dataclass

ALL_ROLES = ("SUPER_ADMIN", "SUB_ADMIN", "HR", "EMPLOYEE")


@dataclass(frozen=True)
class NavItem:
    label: str
    icon: str
    path: str
    roles: tuple[str, ...] = ("SUPER_ADMIN", "SUB_ADMIN", "EMPLOYEE")
    match: str = ""  # highlight the item for every page under this prefix (default: its own path)


ITEMS = [
    NavItem("Dashboard", "layout-dashboard", "/app/dashboard", ALL_ROLES),
    NavItem("Tasks", "list-checks", "/app/tasks"),  # HR has no tasks and no files
    NavItem("Files", "folder", "/files/mine", match="/files"),
    NavItem("Chat", "message-circle", "/app/chat", ALL_ROLES, match="/app/chat"),
    NavItem("Attendance", "calendar", "/app/attendance", ALL_ROLES),
    NavItem("People", "users", "/app/people", ("SUPER_ADMIN", "HR", "SUB_ADMIN")),
    NavItem("Org chart", "user", "/app/org", ("SUPER_ADMIN", "HR", "SUB_ADMIN")),
    NavItem("Audit", "shield", "/audit", ALL_ROLES),
    NavItem("Displays", "tv", "/app/displays", ("SUPER_ADMIN",)),
    NavItem("Settings", "settings", "/shells/settings", ("SUPER_ADMIN",)),
]


def items_for(role: str | None) -> list[NavItem]:
    # Until accounts exist (M02) the shells show the Super Admin navigation.
    r = role or "SUPER_ADMIN"
    return [i for i in ITEMS if r in i.roles]
