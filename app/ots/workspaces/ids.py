"""File ID format (IMP-04 section 2.3, changed by D-096 / IMP-15.3): `FTS-{PREFIX}-{six digits}`.

The prefix abbreviates the name when the ID is made (AZ Exports -> AZE). The digits come from `secrets`.
The ID is permanent: renaming the company never changes it, and no access depends on it.
"""

from __future__ import annotations

import re
import secrets

PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9-]{3,39}$")  # any File ID that already exists stays valid
STANDARD = re.compile(r"^FTS-[A-Z]{2,4}-\d{6}$")  # what a new or typed-in ID must look like
LEGAL = {"PVT", "PRIVATE", "LTD", "LIMITED", "LLP", "LLC", "INC", "INCORPORATED", "CORP", "CORPORATION"}


def prefix(name: str) -> str:
    """2 to 4 capital letters. Several words: a word of one or two letters stays whole, a longer word gives
    its first letter (AZ Exports -> AZE, Prop Organic Foods -> POF, ABC Trading Company -> ATC). One word: its
    first four letters. Legal endings (Pvt, Ltd, LLP ...) are ignored."""
    every = re.findall(r"[A-Z]+", (name or "").upper())
    words = [w for w in every if w not in LEGAL] or every
    if not words:
        return "XX"
    out = words[0][:4] if len(words) == 1 else "".join(w if len(w) <= 2 else w[0] for w in words)
    out = out[:4]
    return out if len(out) >= 2 else (out + "X")[:2]


def generate(name: str = "") -> str:
    """`FTS-AZE-483921`. The caller retries on a collision (the database decides uniqueness)."""
    return f"FTS-{prefix(name)}-{secrets.randbelow(1_000_000):06d}"


def normalise(raw: str) -> str:
    """Trim, upper-case, and turn runs of spaces into one hyphen. Not yet checked against the pattern."""
    return re.sub(r"\s+", "-", (raw or "").strip()).upper()


def is_valid(norm: str) -> bool:
    return bool(PATTERN.match(norm)) and "--" not in norm


def is_standard(norm: str) -> bool:
    return bool(STANDARD.match(norm))
