"""The Files section strip (IMP-06.1): built from the path, so every /files page shows the same strip,
the right section lit, with nothing to pass in. HR has no Files area at all."""

from __future__ import annotations

from typing import Any

SECTIONS = [
    ("mine", "My storage", "/files/mine"),
    ("tasks", "Task files", "/files/tasks"),
    ("ids", "File IDs", "/files/ids"),
    ("library", "Library", "/files/library"),
    ("shared", "Shared with me", "/files/shared"),
]


def active_section(path: str) -> str:
    for key, _label, url in [*SECTIONS, ("deleted", "", "/files/deleted"), ("people", "", "/files/people")]:
        if path == url or path.startswith(url + "/"):
            return key
    return ""


def files_strip(user: Any, path: str) -> list[dict[str, Any]] | None:
    """None outside the Files area and for anyone without it."""
    from ots.accounts.models import Role

    if not path.startswith("/files") or getattr(user, "role", None) not in (
        Role.SUPER_ADMIN,
        Role.SUB_ADMIN,
        Role.EMPLOYEE,
    ):
        return None
    on = active_section(path)
    tabs = list(SECTIONS)
    if user.role == Role.SUPER_ADMIN:
        tabs.append(("people", "People's storage", "/files/people"))
        tabs.append(("deleted", "Deleted", "/files/deleted"))
    return [{"key": k, "label": label, "url": url, "on": k == on} for k, label, url in tabs]
