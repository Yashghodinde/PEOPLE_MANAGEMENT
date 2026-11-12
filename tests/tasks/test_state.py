"""M07.2: the table as data, the derived status, and the employee-facing labels."""

import pytest
from ots.tasks import state
from ots.tasks.models import AssignmentStatus as S
from ots.tasks.models import BlockReason
from ots.tasks.models import DerivedStatus as D


def test_the_table_has_every_action_from_the_plan():
    assert set(state.TRANSITIONS) == {
        "start", "pause_eod", "resume", "block", "unblock", "submit", "request_changes", "approve", "remove", "cancel",
    }  # fmt: skip


def test_no_transition_leaves_a_final_state():
    for t in state.TRANSITIONS.values():
        assert not (t.sources & state.FINAL), t.action
    assert state.FINAL == {S.APPROVED, S.REMOVED, S.CANCELLED}


def test_every_transition_names_an_event_and_ends_in_a_known_state():
    for t in state.TRANSITIONS.values():
        assert t.event.startswith("assignment.") and t.target in S.values


def test_remove_and_cancel_work_from_every_non_final_state():
    assert state.TRANSITIONS["remove"].sources == state.NON_FINAL == state.TRANSITIONS["cancel"].sources


def test_allowed_actions_for_building_buttons():
    assert state.allowed_actions(S.ASSIGNED) == ["start", "remove", "cancel"]
    assert "approve" in state.allowed_actions(S.SUBMITTED) and "start" not in state.allowed_actions(
        S.SUBMITTED
    )
    assert state.allowed_actions(S.APPROVED) == []


@pytest.mark.parametrize(
    "statuses,cancelled,expected,blocked",
    [
        ([S.APPROVED, S.APPROVED], False, D.COMPLETED, False),
        ([S.APPROVED, S.REMOVED], False, D.COMPLETED, False),  # a removed person does not hold the task back
        ([S.REMOVED], False, D.ASSIGNED, False),  # nobody left: not "complete", just unassigned
        ([], False, D.ASSIGNED, False),
        ([S.APPROVED, S.SUBMITTED], False, D.AWAITING_REVIEW, False),
        ([S.SUBMITTED, S.IN_PROGRESS], False, D.AWAITING_REVIEW, False),  # SUBMITTED outranks IN_PROGRESS
        ([S.IN_PROGRESS, S.ASSIGNED], False, D.IN_PROGRESS, False),
        ([S.CHANGES_REQUESTED], False, D.IN_PROGRESS, False),
        ([S.PAUSED], False, D.IN_PROGRESS, False),
        ([S.BLOCKED, S.ASSIGNED], False, D.IN_PROGRESS, True),
        ([S.ASSIGNED, S.ASSIGNED], False, D.ASSIGNED, False),
        ([S.APPROVED, S.IN_PROGRESS], False, D.IN_PROGRESS, False),
        ([S.IN_PROGRESS], True, D.CANCELLED, False),  # cancelled wins over everything
        ([S.APPROVED, S.APPROVED], True, D.CANCELLED, False),
    ],
)
def test_derived_status_follows_the_plans_order(statuses, cancelled, expected, blocked):
    assert state.derive_status([str(s) for s in statuses], cancelled) == (expected, blocked)


@pytest.mark.parametrize(
    "status,reason,label",
    [
        (S.ASSIGNED, "", "Not started"),
        (S.IN_PROGRESS, "", "Working"),
        (S.PAUSED, "", "Paused: office time over"),
        (S.BLOCKED, BlockReason.INSUFFICIENT_INPUT, "Waiting: insufficient input from manager or senior"),
        (S.BLOCKED, BlockReason.DEPENDENCY, "Waiting: other dependency"),
        (S.SUBMITTED, "", "Completed (awaiting review)"),
        (S.CHANGES_REQUESTED, "", "Changes requested"),
        (S.APPROVED, "", "Approved"),
    ],
)
def test_employee_labels_match_the_plan(status, reason, label):
    assert state.employee_label(status, reason) == label
