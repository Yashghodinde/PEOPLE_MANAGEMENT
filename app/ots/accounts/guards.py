"""Other modules can stop a person being deactivated while they still hold something (F-TK-5).

A guard takes the person and returns plain-language lines describing what is still open ("T-2026-000007
Monthly report"); an empty list means no objection. Tasks registers one; Files may add one later.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

Guard = Callable[[Any], list[str]]
_GUARDS: list[Guard] = []


def register(guard: Guard) -> None:
    if guard not in _GUARDS:
        _GUARDS.append(guard)


def objections(user: Any) -> list[str]:
    return [line for guard in _GUARDS for line in guard(user)]
