"""The page header trail (IMP-14.1): built from the path so every page shows the same breadcrumb and Back,
with nothing to pass in. The last crumb is the page's own title; earlier ones are fixed sections."""

from __future__ import annotations

import re

_FILES = ("Files", "/files/mine")
_SECTIONS = {
    "mine": ("My storage", "/files/mine"),
    "tasks": ("Task files", "/files/tasks"),
    "ids": ("File IDs", "/files/ids"),
    "library": ("Library", "/files/library"),
    "shared": ("Shared with me", "/files/shared"),
    "deleted": ("Deleted", "/files/deleted"),
}
_TASKS = ("Tasks", "/app/tasks")


def trail(path: str, title: str) -> list[tuple[str, str]]:
    """[(label, url)] ending with the current page (empty url). Fewer than two entries means no header."""
    path = path.rstrip("/") or "/"
    parts = [p for p in path.split("/") if p]
    out: list[tuple[str, str]] = []
    if path.startswith("/files"):
        out.append(_FILES)
        key = parts[1] if len(parts) > 1 else ""
        if len(parts) <= 2 and key in _SECTIONS:
            return []  # a section's own page: the section strip already says where you are
        if key in _SECTIONS:
            out.append(_SECTIONS[key])
        elif key == "search":
            out.append(("Search", "/files/search"))
        if len(parts) > 2 or key not in _SECTIONS:
            out.append((title or "", ""))
        else:
            out[-1] = (out[-1][0], "")
    elif path.startswith("/app/tasks") and len(parts) > 2:
        out = [_TASKS]
        if parts[2] == "all":
            out.append(("All tasks", ""))
        elif parts[2] == "new":
            out.append(("New task", ""))
        else:
            out.append((title or "Task", ""))
    elif re.match(r"^/app/people/.+", path):
        out = [("People", "/app/people"), (title or "Person", "")]
    elif path == "/app/org":
        out = [("People", "/app/people"), ("Org chart", "")]
    out = [(a, b) for a, b in out if a]
    if len(out) > 1 and out[-1][1]:
        out[-1] = (out[-1][0], "")
    return out if len(out) > 1 else []
