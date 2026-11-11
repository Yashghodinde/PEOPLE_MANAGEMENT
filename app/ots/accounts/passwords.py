"""Password policy (M02 rule 11): >= 10 chars, not the login id, not in the bundled common list."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from ots.core.errors import ValidationFailed

MIN_LENGTH = 10
COMMON_FILE = Path(__file__).parent / "data" / "common_passwords.txt"


@lru_cache(maxsize=1)
def _common() -> frozenset[str]:
    if not COMMON_FILE.exists():
        return frozenset()
    return frozenset(x.strip().lower() for x in COMMON_FILE.read_text().splitlines() if x.strip())


def validate(password: str, login_id: str = "") -> None:
    problems = []
    if len(password) < MIN_LENGTH:
        problems.append(f"Use at least {MIN_LENGTH} characters.")
    if login_id and password.lower() == login_id.lower():
        problems.append("The password must not be the same as the login ID.")
    if password.lower() in _common():
        problems.append("This password is too common. Choose another.")
    if problems:
        raise ValidationFailed(" ".join(problems), fields={"password": problems})
