"""The task state machine (M07 section 2): the single source of truth, as data, not if-chains.

Each `Transition` says where an action may start, where it ends, who may do it, what the person must
supply and which event it produces. `services.transition()` is the only code that applies one; the
permission rules live in `permissions.py`, and the derived task status is computed here.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import AssignmentStatus as S
from .models import BlockReason, DerivedStatus

FINAL = frozenset({S.APPROVED, S.REMOVED, S.CANCELLED})
NON_FINAL = frozenset(S) - FINAL

MIN_BLOCK_NOTE = 5
MIN_SUBMIT_NOTE = 10
MIN_FEEDBACK = 10
MIN_REASON = 5


@dataclass(frozen=True)
class Transition:
    action: str
    sources: frozenset[S]
    target: S
    actors: str  # who may do it (documentation; the rule itself is the policy `assignment.<action>`)
    event: str
    inputs: tuple[str, ...] = ()  # input names this action needs, validated by the engine


TRANSITIONS: dict[str, Transition] = {
    t.action: t
    for t in (
        Transition("start", frozenset({S.ASSIGNED}), S.IN_PROGRESS, "assignee", "assignment.started"),
        Transition(
            "pause_eod", frozenset({S.IN_PROGRESS}), S.PAUSED, "assignee or system", "assignment.paused"
        ),
        Transition(
            "resume",
            frozenset({S.PAUSED, S.CHANGES_REQUESTED}),
            S.IN_PROGRESS,
            "assignee",
            "assignment.resumed",
        ),
        Transition(
            "block",
            frozenset({S.IN_PROGRESS, S.PAUSED}),
            S.BLOCKED,
            "assignee",
            "assignment.blocked",
            ("block_reason", "block_note"),
        ),
        Transition(
            "unblock",
            frozenset({S.BLOCKED}),
            S.IN_PROGRESS,
            "assignee, task creator or Super Admin",
            "assignment.unblocked",
        ),
        Transition(
            "submit",
            frozenset({S.IN_PROGRESS, S.PAUSED, S.CHANGES_REQUESTED}),
            S.SUBMITTED,
            "assignee",
            "assignment.submitted",
            ("note",),
        ),
        Transition(
            "request_changes",
            frozenset({S.SUBMITTED}),
            S.CHANGES_REQUESTED,
            "reviewer",
            "assignment.changes_requested",
            ("feedback",),
        ),
        Transition("approve", frozenset({S.SUBMITTED}), S.APPROVED, "reviewer", "assignment.approved"),
        Transition(
            "remove", NON_FINAL, S.REMOVED, "task creator or Super Admin", "assignment.removed", ("reason",)
        ),
        Transition(
            "cancel",
            NON_FINAL,
            S.CANCELLED,
            "task creator or Super Admin",
            "assignment.cancelled",
            ("reason",),
        ),
    )
}


def allowed_actions(status: str) -> list[str]:
    """Actions that can start from `status`, in table order (for building buttons)."""
    return [a for a, t in TRANSITIONS.items() if status in t.sources]


def derive_status(statuses: list[str], is_cancelled: bool) -> tuple[str, bool]:
    """The task's cached status from its assignments, in the plan's order. Returns (status, has_blocked)."""
    active = [s for s in statuses if s not in (S.REMOVED, S.CANCELLED)]
    has_blocked = any(s == S.BLOCKED for s in active)
    if is_cancelled:
        return DerivedStatus.CANCELLED, has_blocked
    if active and all(s == S.APPROVED for s in active):
        return DerivedStatus.COMPLETED, has_blocked
    if any(s == S.SUBMITTED for s in active):
        return DerivedStatus.AWAITING_REVIEW, has_blocked
    if any(s in (S.IN_PROGRESS, S.CHANGES_REQUESTED, S.PAUSED, S.BLOCKED) for s in active):
        return DerivedStatus.IN_PROGRESS, has_blocked
    return DerivedStatus.ASSIGNED, has_blocked


# What the assignee sees (M07 section 2): friendly, and never blames anyone by accident.
EMPLOYEE_LABELS = {
    S.ASSIGNED: "Not started",
    S.IN_PROGRESS: "Working",
    S.PAUSED: "Paused: office time over",
    S.SUBMITTED: "Completed (awaiting review)",
    S.CHANGES_REQUESTED: "Changes requested",
    S.APPROVED: "Approved",
    S.REMOVED: "Removed from this task",
    S.CANCELLED: "Task cancelled",
}
BLOCKED_LABELS = {
    BlockReason.INSUFFICIENT_INPUT: "Waiting: insufficient input from manager or senior",
    BlockReason.DEPENDENCY: "Waiting: other dependency",
}


def employee_label(status: str, block_reason: str = "") -> str:
    if status == S.BLOCKED:
        return BLOCKED_LABELS.get(BlockReason(block_reason), "Waiting") if block_reason else "Waiting"
    return EMPLOYEE_LABELS.get(S(status), status)
