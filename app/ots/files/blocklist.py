"""Blocked file types (M08 section 2, F-FL-8).

Programs that run on a double click are refused at upload. The Super Admin may allow one upload anyway;
that override is audited by the upload service (M08.2), not here.
"""

from __future__ import annotations

BLOCKED_EXTENSIONS = frozenset({"exe", "msi", "bat", "cmd", "ps1", "vbs", "js", "jar", "scr", "lnk", "com"})


def extension_of(filename: str) -> str:
    """The lower-case extension without the dot, or '' when there is none. 'a.tar.GZ' gives 'gz'."""
    name = filename.strip().rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if "." not in name.strip("."):
        return ""
    return name.rsplit(".", 1)[-1].strip().lower()


def is_blocked(filename: str) -> bool:
    return extension_of(filename) in BLOCKED_EXTENSIONS
