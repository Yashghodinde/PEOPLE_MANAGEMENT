"""M07.2: the engine applies the table, validates inputs, keeps time, applies the reviewer rule and cache."""

import datetime

import pytest
from freezegun import freeze_time
from ots.accounts.models import Role
from ots.core.errors import Conflict, InvalidTransition, NotFound, PermissionDenied, ValidationFailed
from ots.core.models import OutboxEvent
from ots.core.permissions import can
from ots.tasks import hooks, services, state
from ots.tasks.models import AssignmentStatus as S
from ots.tasks.models import DerivedStatus as D
from ots.tasks.models import Task, TaskAssignment, TaskEvent

from tests.accounts.factories import make_user
from tests.tasks.factories import make_task

pytestmark = pytest.mark.django_db

INPUTS = {
    "block": {"block_reason": "DEPENDENCY", "block_note": "waiting for the bank statement"},
    "submit": {"note": "All figures checked and sent."},
    "request_changes": {"feedback": "Please add the October column."},
    "remove": {"reason": "reassigned to another person"},
    "cancel": {"reason": "no longer needed"},
}
# the person who is allowed to do each action, by key in the `org` fixture
WHO = {
    "start": "emp", "pause_eod": "emp", "resume": "emp", "block": "emp", "unblock": "emp", "submit": "emp",
    "request_changes": "mgr", "approve": "mgr", "remove": "mgr", "cancel": "mgr",
}  # fmt: skip


def put_in(a: TaskAssignment, status: str) -> TaskAssignment:
    """Set an assignment to a state directly (as it would be after the earlier steps)."""
    extra = {}
    if status == S.BLOCKED:
        extra = {
            "block_reason": "DEPENDENCY",
            "block_note": "waiting",
            "blocked_since": datetime.datetime.now(datetime.UTC),
        }
    TaskAssignment.objects.filter(pk=a.pk).update(status=status, **extra)
    a.refresh_from_db()
    return a


# --- every (state, action) pair ---------------------------------------------------------------------------------


@pytest.mark.parametrize("action", sorted(state.TRANSITIONS))
@pytest.mark.parametrize("status", [str(s) for s in S])
def test_every_state_and_action_pair(org, task, assignment, status, action):
    a = put_in(assignment, status)
    actor = org[WHO[action]]
    events_before = TaskEvent.objects.count()
    trans = state.TRANSITIONS[action]
    if status in trans.sources:
        out = services.transition(a.pk, action, actor, a.version, **INPUTS.get(action, {}))
        assert out.status == trans.target and out.version == a.version + 1
        assert TaskEvent.objects.filter(event_type=trans.event, assignment=a).count() == 1
    else:
        with pytest.raises(InvalidTransition):
            services.transition(a.pk, action, actor, a.version, **INPUTS.get(action, {}))
        a.refresh_from_db()
        assert (
            a.status == status and TaskEvent.objects.count() == events_before
        )  # nothing changed, nothing logged


def test_unknown_action_and_missing_assignment(org, assignment):
    with pytest.raises(InvalidTransition):
        services.transition(assignment.pk, "teleport", org["emp"])
    import uuid

    with pytest.raises(NotFound):
        services.transition(uuid.uuid4(), "start", org["emp"])


# --- what each action records ---------------------------------------------------------------------------------


def test_start_stamps_the_first_start_and_the_event_carries_the_story(org, task, assignment):
    with freeze_time("2030-01-01 05:00:00"):
        a = services.transition(assignment.pk, "start", org["emp"], assignment.version)
    assert a.started_at.isoformat().startswith("2030-01-01T05:00:00")
    ev = TaskEvent.objects.get(assignment=a, event_type="assignment.started")
    assert (
        ev.actor_id == org["emp"].pk
        and ev.payload["from"] == "ASSIGNED"
        and ev.payload["to"] == "IN_PROGRESS"
    )


def test_pause_for_the_day_and_resume(org, assignment):
    a = services.transition(assignment.pk, "start", org["emp"], 1)
    a = services.transition(a.pk, "pause_eod", org["emp"], a.version)
    assert (a.status, a.pause_reason) == (S.PAUSED, "END_OF_DAY")
    a = services.transition(a.pk, "resume", org["emp"], a.version)
    assert (a.status, a.pause_reason) == (S.IN_PROGRESS, "")


def test_the_system_may_pause_for_end_of_day_but_do_nothing_else(org, assignment):
    a = put_in(assignment, S.IN_PROGRESS)
    assert services.transition(a.pk, "pause_eod", None, a.version, system=True).status == S.PAUSED
    b = put_in(assignment, S.SUBMITTED)
    with pytest.raises(PermissionDenied):
        services.transition(b.pk, "approve", None, b.version, system=True)


def test_blocking_needs_a_reason_and_a_note_and_time_is_counted(org, assignment):
    a = put_in(assignment, S.IN_PROGRESS)
    for bad in (
        {},
        {"block_reason": "WHATEVER", "block_note": "long enough note"},
        {"block_reason": "DEPENDENCY", "block_note": "no"},
    ):
        with pytest.raises(ValidationFailed):
            services.transition(a.pk, "block", org["emp"], a.version, **bad)
    with freeze_time("2030-01-01 05:00:00") as clock:
        a.refresh_from_db()
        a = services.transition(a.pk, "block", org["emp"], a.version, **INPUTS["block"])
        assert (a.status, a.block_reason) == (S.BLOCKED, "DEPENDENCY") and a.blocked_since is not None
        clock.tick(datetime.timedelta(minutes=90))
        a = services.transition(a.pk, "unblock", org["mgr"], a.version, note="statement arrived")
        assert (a.status, a.blocked_seconds, a.blocked_since, a.block_reason) == (
            S.IN_PROGRESS,
            5400,
            None,
            "",
        )
        a = services.transition(
            a.pk,
            "block",
            org["emp"],
            a.version,
            block_reason="INSUFFICIENT_INPUT",
            block_note="need a decision",
        )
        clock.tick(datetime.timedelta(minutes=10))
        a = services.transition(a.pk, "unblock", org["emp"], a.version)
    assert a.blocked_seconds == 6000  # both waits are added up


def test_removing_someone_who_is_waiting_closes_the_wait(org, assignment):
    with freeze_time("2030-01-01 05:00:00") as clock:
        a = put_in(assignment, S.IN_PROGRESS)
        a = services.transition(a.pk, "block", org["emp"], a.version, **INPUTS["block"])
        clock.tick(datetime.timedelta(minutes=30))
        a = services.transition(a.pk, "remove", org["mgr"], a.version, reason="moved to another team")
    assert (a.status, a.blocked_seconds, a.blocked_since) == (
        S.REMOVED,
        1800,
        None,
    ) and a.removed_at is not None


def test_submitting_needs_a_note_or_a_file_and_flags_lateness(org, task, assignment):
    a = put_in(assignment, S.IN_PROGRESS)
    for bad in ({}, {"note": "too short"}):
        with pytest.raises(ValidationFailed):
            services.transition(a.pk, "submit", org["emp"], a.version, **bad)
    due = task.due_at
    with freeze_time(due - datetime.timedelta(hours=1)):
        ok = services.transition(a.pk, "submit", org["emp"], a.version, **INPUTS["submit"])
    assert ok.submitted_late is False and ok.submitted_at is not None
    late = put_in(make_task(org["mgr"], org["peer"]).assignments.get(), S.IN_PROGRESS)
    with freeze_time(late.task.due_at + datetime.timedelta(minutes=1)):
        assert (
            services.transition(
                late.pk, "submit", org["peer"], late.version, **INPUTS["submit"]
            ).submitted_late
            is True
        )


def test_a_linked_submission_file_replaces_the_note(org, assignment, monkeypatch):
    a = put_in(assignment, S.IN_PROGRESS)
    monkeypatch.setattr(hooks, "has_submission_file", lambda assignment: True)
    assert services.transition(a.pk, "submit", org["emp"], a.version).status == S.SUBMITTED


def test_review_loop_changes_requested_then_resubmit_then_approve(org, task, assignment):
    a = put_in(assignment, S.SUBMITTED)
    with pytest.raises(ValidationFailed):
        services.transition(a.pk, "request_changes", org["mgr"], a.version, feedback="no")
    a = services.transition(a.pk, "request_changes", org["mgr"], a.version, **INPUTS["request_changes"])
    assert a.status == S.CHANGES_REQUESTED
    a = services.transition(
        a.pk, "submit", org["emp"], a.version, **INPUTS["submit"]
    )  # straight back to review
    a = services.transition(a.pk, "approve", org["mgr"], a.version)
    assert a.status == S.APPROVED and a.approved_at is not None
    task.refresh_from_db()
    assert task.derived_status == D.COMPLETED and task.completed_at is not None


# --- permissions: the matrix for assignment actions ------------------------------------------------------------


def relations(org, task, assignment):
    sa, mgr, other, emp, peer, outsider = (org[k] for k in ("sa", "mgr", "other", "emp", "peer", "outsider"))
    creator_other = make_task(other, emp).assignments.get(
        user=emp
    )  # created by `other`, not by emp's manager
    return {
        "assignee": (emp, assignment),
        "creator_sub": (mgr, assignment),
        "manager_not_creator": (mgr, creator_other),
        "super_admin": (sa, assignment),
        "other_sub": (other, assignment),
        "peer_employee": (peer, assignment),
    }


MATRIX = {
    "start": {"assignee"}, "pause_eod": {"assignee"}, "resume": {"assignee"}, "block": {"assignee"},
    "submit": {"assignee"},
    "unblock": {"assignee", "creator_sub", "super_admin"},
    "approve": {"creator_sub", "manager_not_creator", "super_admin"},
    "request_changes": {"creator_sub", "manager_not_creator", "super_admin"},
    "remove": {"creator_sub", "super_admin"}, "cancel": {"creator_sub", "super_admin"},
}  # fmt: skip


@pytest.mark.parametrize("action", sorted(MATRIX))
def test_assignment_permission_matrix(org, task, assignment, action):
    for name, (user, a) in relations(org, task, assignment).items():
        a = TaskAssignment.objects.select_related("task", "user").get(pk=a.pk)
        assert can(user, f"assignment.{action}", a) is (name in MATRIX[action]), f"{name} / {action}"


def test_an_assignee_who_is_a_sub_admin_can_never_review_their_own_work(org):
    boss_task = make_task(org["sa"], org["mgr"])  # the Super Admin gives work to a Sub-Admin
    a = put_in(boss_task.assignments.get(), S.SUBMITTED)
    with pytest.raises(PermissionDenied):
        services.transition(a.pk, "approve", org["mgr"], a.version)
    with pytest.raises(PermissionDenied):
        services.transition(a.pk, "request_changes", org["mgr"], a.version, **INPUTS["request_changes"])
    assert services.transition(a.pk, "approve", org["sa"], a.version).status == S.APPROVED


def test_a_cancelled_task_accepts_no_more_work(org, task, assignment):
    Task.objects.filter(pk=task.pk).update(is_cancelled=True)
    with pytest.raises(InvalidTransition):
        services.transition(assignment.pk, "start", org["emp"], assignment.version)


def test_a_deactivated_person_cannot_act(org, assignment):
    from ots.accounts.models import User

    User.objects.filter(pk=org["emp"].pk).update(is_active=False)
    emp = User.objects.get(pk=org["emp"].pk)
    with pytest.raises(PermissionDenied):
        services.transition(assignment.pk, "start", emp, assignment.version)


# --- the cache, groups, versions and events -------------------------------------------------------------------


def test_a_group_task_keeps_each_persons_progress_separate(org):
    task = make_task(org["mgr"], org["emp"], org["peer"])
    a_emp, a_peer = (task.assignments.get(user=org[k]) for k in ("emp", "peer"))
    services.transition(a_emp.pk, "start", org["emp"], a_emp.version)
    task.refresh_from_db()
    assert (
        task.derived_status == D.IN_PROGRESS and task.assignments.get(user=org["peer"]).status == S.ASSIGNED
    )
    for who, a in (("emp", a_emp), ("peer", a_peer)):
        a.refresh_from_db()
        if a.status == S.ASSIGNED:
            a = services.transition(a.pk, "start", org[who], a.version)
        a = services.transition(a.pk, "submit", org[who], a.version, **INPUTS["submit"])
    task.refresh_from_db()
    assert task.derived_status == D.AWAITING_REVIEW
    a_emp.refresh_from_db()
    services.transition(a_emp.pk, "approve", org["mgr"], a_emp.version)
    task.refresh_from_db()
    assert (
        task.derived_status == D.AWAITING_REVIEW and task.completed_at is None
    )  # one person still to review
    a_peer.refresh_from_db()
    services.transition(a_peer.pk, "approve", org["mgr"], a_peer.version)
    task.refresh_from_db()
    assert task.derived_status == D.COMPLETED and task.completed_at is not None


def test_has_blocked_follows_the_waiting_assignments(org, task, assignment):
    a = put_in(assignment, S.IN_PROGRESS)
    a = services.transition(a.pk, "block", org["emp"], a.version, **INPUTS["block"])
    task.refresh_from_db()
    assert task.has_blocked is True
    services.transition(a.pk, "unblock", org["emp"], a.version)
    task.refresh_from_db()
    assert task.has_blocked is False


def test_every_transition_bumps_both_versions_and_publishes_an_event(org, task, assignment):
    tv = task.version
    a = services.transition(assignment.pk, "start", org["emp"], assignment.version)
    task.refresh_from_db()
    assert a.version == assignment.version + 1 and task.version == tv + 1
    ev = OutboxEvent.objects.get(type="assignment.started")
    assert ev.payload["assignee_id"] == str(org["emp"].pk) and ev.payload["creator_id"] == str(org["mgr"].pk)
    assert ev.payload["manager_id"] == str(org["mgr"].pk) and ev.payload["task_code"] == task.task_code


def test_a_stale_version_is_a_conflict_that_shows_the_current_state(org, assignment):
    services.transition(assignment.pk, "start", org["emp"], assignment.version)
    with pytest.raises(Conflict) as exc:
        services.transition(
            assignment.pk, "start", org["emp"], assignment.version
        )  # the page was out of date
    assert (
        exc.value.extra["current"]["status"] == "IN_PROGRESS" and exc.value.extra["current"]["version"] == 2
    )
    assert exc.value.status == 409


def test_the_assignee_label_is_in_the_conflict_snapshot(org, assignment):
    a = put_in(assignment, S.IN_PROGRESS)
    a = services.transition(a.pk, "block", org["emp"], a.version, **INPUTS["block"])
    with pytest.raises(Conflict) as exc:
        services.transition(a.pk, "unblock", org["emp"], a.version - 1)
    assert exc.value.extra["current"]["label"] == "Waiting: other dependency"


def test_nobody_can_approve_work_they_assigned_to_themselves(org):
    """The guard that matters: the creator or the Super Admin doing the work must not review it."""
    for who, reviewer in (("mgr", None), ("sa", None)):
        own = make_task(org[who], org[who])  # created by, and assigned to, the same person
        a = put_in(own.assignments.get(), S.SUBMITTED)
        for action, extra in (("approve", {}), ("request_changes", INPUTS["request_changes"])):
            with pytest.raises(PermissionDenied):
                services.transition(a.pk, action, org[who], a.version, **extra)
        assert TaskAssignment.objects.get(pk=a.pk).status == S.SUBMITTED
        reviewer = org["sa"] if who == "mgr" else make_user(Role.SUPER_ADMIN, login_id="boss2")
        assert (
            services.transition(a.pk, "approve", reviewer, a.version).status == S.APPROVED
        )  # someone else can
