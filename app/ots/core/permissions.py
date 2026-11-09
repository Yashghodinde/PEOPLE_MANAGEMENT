"""Policy registry. Modules register policies with @policy('task.view'); require() fails closed."""

from collections.abc import Callable
from typing import Any

from .errors import PermissionDenied

PolicyFn = Callable[[Any, Any], bool]
_REGISTRY: dict[str, PolicyFn] = {}


class UnknownAction(PermissionDenied):
    code = "unknown_action"


def policy(action: str) -> Callable[[PolicyFn], PolicyFn]:
    def register(fn: PolicyFn) -> PolicyFn:
        if action in _REGISTRY:
            raise RuntimeError(f"policy already registered: {action}")
        _REGISTRY[action] = fn
        return fn

    return register


def is_registered(action: str) -> bool:
    return action in _REGISTRY


def registered_actions() -> list[str]:
    return sorted(_REGISTRY)


def can(user: Any, action: str, obj: Any = None) -> bool:
    fn = _REGISTRY.get(action)
    if fn is None:
        return False
    return bool(fn(user, obj))


def require(user: Any, action: str, obj: Any = None) -> None:
    fn = _REGISTRY.get(action)
    if fn is None:
        raise UnknownAction(f"no policy registered for '{action}'")
    if not fn(user, obj):
        raise PermissionDenied(f"not allowed: {action}", action=action)
