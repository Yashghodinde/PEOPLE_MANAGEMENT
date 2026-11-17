"""File ID format (IMP-04 section 2.3). Generated ids carry nothing: no name, sequence or date."""

from __future__ import annotations

import re
import secrets

ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZ23456789"  # no O, 0, I, 1, L or U
PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9-]{3,39}$")


def generate() -> str:
    """FTS- plus two groups of four from the alphabet, drawn with `secrets`."""
    group = lambda: "".join(secrets.choice(ALPHABET) for _ in range(4))  # noqa: E731
    return f"FTS-{group()}-{group()}"


def normalise(raw: str) -> str:
    """Trim, upper-case, and turn runs of spaces into one hyphen. Not yet checked against the pattern."""
    return re.sub(r"\s+", "-", (raw or "").strip()).upper()


def is_valid(norm: str) -> bool:
    return bool(PATTERN.match(norm)) and "--" not in norm
