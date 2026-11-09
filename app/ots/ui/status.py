"""Fixed mapping from enum values to badge tone (ok / warn / bad / neutral). One place, tested."""

OK = {"APPROVED", "COMPLETED", "ARCHIVED", "SUCCESS", "DONE", "P", "ACKNOWLEDGED", "RESOLVED"}
WARN = {
    "SUBMITTED", "AWAITING_REVIEW", "PAUSED", "BLOCKED", "L", "RETRY_WAIT", "PARTIAL", "STAGED",
    "ARCHIVING", "VERIFYING", "WARNING", "PENDING", "RECEIVING", "TAB_ONLY", "CHANGES_REQUESTED",
    "BLOCKED_QUOTA", "BLOCKED_AUTH",
}  # fmt: skip
BAD = {
    "FAILED", "MISSING", "DRIFTED", "REJECTED", "CANCELLED", "A", "CRITICAL", "CONFLICT", "ABANDONED",
}  # fmt: skip


def tone(value: str) -> str:
    v = str(value).upper()
    if v in OK:
        return "ok"
    if v in BAD:
        return "bad"
    if v in WARN:
        return "warn"
    return "neutral"


def label(value: str) -> str:
    return str(value).replace("_", " ").capitalize()
