"""File codes (M08 section 1).

    {YYYYMMDD}-{HHMMSS}_{EMPID}-{FirstnameL}_{TASKCODE|LIB}-{slug}_v{N}.{ext}
    20261109-154312_EMP003-RahulS_T-2026-000123-monthly-sales-report_v2.xlsx

Only [A-Za-z0-9._-] ever appears. The time is the server's commit time in IST. `make_code` is a pure
function; the unique check and the `-2` suffix for a rare collision happen in `unique_code`.
"""

from __future__ import annotations

import datetime
import re
import unicodedata
from collections.abc import Callable

from ots.core.clock import IST

SLUG_MAX = 40
EXT_MAX = 10
EMP_MAX = 20
PERSON_MAX = 24
_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def ascii_fold(text: str) -> str:
    """'Zoë Ñandú' becomes 'Zoe Nandu'. Characters with no ASCII form (for example CJK) are dropped."""
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


def slugify(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", ascii_fold(title).lower()).strip("-")
    return s[:SLUG_MAX].strip("-")


def person_part(full_name: str) -> str:
    """First name plus the last initial: 'Rahul Sharma' gives 'RahulS'. One name gives just that name."""
    words = [re.sub(r"[^A-Za-z0-9]", "", ascii_fold(w)) for w in full_name.split()]
    words = [w for w in words if w]
    if not words:
        return "User"
    first = words[0][0].upper() + words[0][1:]
    out = first[: PERSON_MAX - 1] + words[-1][0].upper() if len(words) > 1 else first[:PERSON_MAX]
    return out


def clean_extension(filename: str) -> str:
    name = filename.replace("\\", "/").rsplit("/", 1)[-1].rstrip(". ").strip()
    if "." not in name.strip("."):
        return "bin"
    ext = re.sub(r"[^a-z0-9]", "", ascii_fold(name.rsplit(".", 1)[-1]).lower())
    return ext if 0 < len(ext) <= EXT_MAX else "bin"


def make_code(
    *,
    committed_at: datetime.datetime,
    employee_id: str,
    full_name: str,
    task_code: str | None,
    title: str,
    version: int,
    original_filename: str,
    place: str | None = None,
) -> str:
    local = committed_at.astimezone(IST)
    emp = _SAFE.sub("", ascii_fold(employee_id))[:EMP_MAX] or "EMP"
    # the task's code; PERS for a personal file (IMP-02); LIB for the library
    where = _SAFE.sub("", ascii_fold(task_code))[:24] if task_code else (place or "LIB")
    slug = slugify(title) or "untitled"
    code = (
        f"{local:%Y%m%d-%H%M%S}_{emp}-{person_part(full_name)}_{where}-{slug}"
        f"_v{int(version)}.{clean_extension(original_filename)}"
    )
    return _SAFE.sub("", code)


def unique_code(code: str, taken: Callable[[str], bool]) -> str:
    """Return `code`, or `code` with -2, -3 ... before the extension, until `taken` says it is free."""
    if not taken(code):
        return code
    stem, dot, ext = code.rpartition(".")
    n = 2
    while taken(candidate := f"{stem}-{n}{dot}{ext}"):
        n += 1
    return candidate
