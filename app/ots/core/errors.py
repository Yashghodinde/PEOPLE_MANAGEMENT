from typing import Any


class OtsError(Exception):
    status = 500
    code = "error"

    def __init__(self, message: str = "", **extra: Any) -> None:
        super().__init__(message or self.code)
        self.message = message or self.code
        self.extra = extra

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.code, "message": self.message, **self.extra}


class PermissionDenied(OtsError):
    status = 403
    code = "permission_denied"


class NotFound(OtsError):
    status = 404
    code = "not_found"


class Conflict(OtsError):
    status = 409
    code = "conflict"

    def __init__(self, message: str = "", current: Any = None, **extra: Any) -> None:
        super().__init__(message, current=current, **extra)


class InvalidTransition(OtsError):
    status = 409
    code = "invalid_transition"


class ValidationFailed(OtsError):
    status = 422
    code = "validation_failed"

    def __init__(self, message: str = "", fields: dict[str, Any] | None = None, **extra: Any) -> None:
        super().__init__(message, fields=fields or {}, **extra)


class Locked(OtsError):
    status = 423
    code = "locked"


class StorageUnavailable(OtsError):
    status = 503
    code = "storage_unavailable"


class QuotaBlocked(OtsError):
    status = 507
    code = "quota_blocked"
