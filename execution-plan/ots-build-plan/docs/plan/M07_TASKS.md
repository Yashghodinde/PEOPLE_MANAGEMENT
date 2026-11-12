# M07 — Tasks, Assignments, State Machine, End-of-Day Closure

**Depends on:** M02, M03, M00.6 (events).
**Produces:**

- tasks and per-person assignments;
- the transition engine;
- derived task status;
- comments;
- end-of-day closure;
- dashboards;
- the M03 timeline provider;
- the M06 day-drawer provider.

**Failure IDs:**

- F-TK-1: double submit or retry.
- F-TK-2: concurrent edit.
- F-TK-3: crash mid-update.
- F-TK-4: illegal transition.
- F-TK-5: deactivated assignee.
- F-TK-6: manager changed after assignment.

## 1. Model rules

- `task_code`: `T-{YYYY}-{6-digit sequence}`. A PostgreSQL sequence per year (`task_code_seq_2026`) is created on demand in a transaction.
- A task has 1 or more assignments. Status lives on `task_assignments`. `tasks.derived_status` is a cache, recomputed in the same transaction as every assignment change.
- `due_at` is required. `scheduled_start_at` is optional.
- `hide_from_display` hides the task from the TV.
- **Search:** `search_vector` is a generated column, `to_tsvector('simple', title || ' ' || description || ' ' || task_code)`, with a GIN index. Also a `pg_trgm` GIN index on `title` for partial matches.

## 2. Transition table (single source of truth, in `tasks/state.py`)

| From | Action | To | Actor | Required input | Server sets | Event |
| --- | --- | --- | --- | --- | --- | --- |
| ASSIGNED | start | IN_PROGRESS | assignee | — | `started_at` (first time) | `assignment.started` |
| IN_PROGRESS | pause_eod | PAUSED (END_OF_DAY) | assignee or system (EOD closure) | — | — | `assignment.paused` |
| PAUSED | resume | IN_PROGRESS | assignee | — | — | `assignment.resumed` |
| IN_PROGRESS, PAUSED | block | BLOCKED | assignee | `block_reason` + `block_note` (≥ 5 chars) | `blocked_since` | `assignment.blocked` (notifies the reports-to manager **and** the task creator) |
| BLOCKED | unblock | IN_PROGRESS | assignee, task creator, or Super Admin | optional note | `blocked_seconds += now − blocked_since`; `blocked_since = null` | `assignment.unblocked` |
| IN_PROGRESS, PAUSED, CHANGES_REQUESTED | submit | SUBMITTED | assignee | ≥ 1 SUBMISSION file linked to this assignment **or** a closing note (≥ 10 chars) | `submitted_at`; `submitted_late = submitted_at > due_at` | `assignment.submitted` |
| SUBMITTED | request_changes | CHANGES_REQUESTED | reviewer | feedback (≥ 10 chars) | — | `assignment.changes_requested` |
| CHANGES_REQUESTED | resume | IN_PROGRESS | assignee | — | — | `assignment.resumed` |
| SUBMITTED | approve | APPROVED | reviewer (never the assignee) | — | `approved_at` | `assignment.approved` |
| any non-final | remove | REMOVED | task creator or Super Admin | reason | `removed_at` | `assignment.removed` |
| any non-final | (task cancel) | CANCELLED | task creator or Super Admin | reason | `tasks.is_cancelled = true` | `task.cancelled` |

**Reviewer:** the task creator, the Super Admin, or the assignee's current manager. The assignee is **never** their own reviewer, even if they are a Sub-Admin.

**Final states:** APPROVED, REMOVED, CANCELLED.

**Derived status, checked in order:**

1. `is_cancelled` → CANCELLED.
2. All active assignments (not REMOVED) are APPROVED → COMPLETED, and set `completed_at`.
3. Any SUBMITTED → AWAITING_REVIEW.
4. Any IN_PROGRESS, CHANGES_REQUESTED, PAUSED or BLOCKED → IN_PROGRESS.
5. Otherwise → ASSIGNED.

`has_blocked` = any assignment is BLOCKED.

**Overdue** is never stored. Selector: `due_at < now() AND status IN (ASSIGNED, IN_PROGRESS, PAUSED, BLOCKED, CHANGES_REQUESTED)`.

**Employee-facing labels:**

| Status | Label |
| --- | --- |
| ASSIGNED | Not started |
| IN_PROGRESS | Working |
| PAUSED | Paused: office time over |
| BLOCKED + INSUFFICIENT_INPUT | Waiting: insufficient input from manager or senior |
| BLOCKED + DEPENDENCY | Waiting: other dependency |
| SUBMITTED | Completed (awaiting review) |
| CHANGES_REQUESTED | Changes requested |
| APPROVED | Approved |

## 3. The transition engine

`services.transition(assignment_id, action, actor, expected_version, **inputs)` does the following:

1. `SELECT … FOR UPDATE` the assignment row and its task row.
2. Version check. A mismatch raises `Conflict` with the current state (F-TK-2).
3. Permission check via `require(actor, f'assignment.{action}', assignment)`.
4. Validate the transition against the table. If invalid, raise `InvalidTransition` (F-TK-4).
5. Validate the inputs.
6. Apply the change and bump the version.
7. Write a `task_events` row with `{from, to, action, inputs}`.
8. Recompute `derived_status` and `has_blocked`; bump the task version.
9. Publish the event. Notifications and the TV are driven by these events.

All of it runs in one `transaction.atomic()` (F-TK-3). The idempotency middleware covers retries (F-TK-1).

**Other task services:**

- `create_task`: creates the task, its assignments, `task_events` `task.created` + `assignment.created` per assignee, and links reference files.
- `update_task`: covers `due_at`, priority, title and description. Writes `task.updated` with a diff; a deadline change also writes `task.deadline_changed` with old and new values.
- `add_assignees` and `reassign`: reassign = `remove` + `add` in one transaction.
- `add_comment`: comments are immutable.

**Assignee validation:**

- The assignee must be active (F-TK-5): deactivation is blocked by M02 if the user has open assignments. The M02 deactivate service lists their open assignments and requires reassignment first.
- A Sub-Admin may assign only within their subtree.

**Manager changes (F-TK-6):** permissions and approvals use the **current** manager. Reports for past periods use `reporting_history`. An assignment keeps `assigned_by` permanently.

## 4. End-of-day closure

- "Log out for the day" opens a modal listing every assignment of the user in IN_PROGRESS, ASSIGNED (if started today), CHANGES_REQUESTED or BLOCKED.
- Each needs one choice:
    - **Completed** → submit, which requires a note or file.
    - **Continue tomorrow** → `pause_eod`.
    - **Waiting** → block, with reason and note. Assignments already BLOCKED can keep their block.
- Submit is one POST carrying all choices plus one idempotency key. The server applies all transitions in **one transaction**: all or nothing. Then it calls the M06 logout with `END_OF_DAY`.
- If any choice is invalid, the response shows field errors and nothing is applied.
- **Next login:** a "Resume today" card lists PAUSED assignments; "Resume all" calls `resume` on each.

## 5. Permissions (tasks)

| Action | SUPER_ADMIN | SUB_ADMIN | EMPLOYEE |
| --- | --- | --- | --- |
| `task.create`, `task.update`, `task.cancel`, `assignment.add`, `assignment.remove` | any | subtree assignees; own tasks | ✗ |
| `task.view` | all | tasks they created, or where any assignee is in their subtree | tasks where they hold or held an assignment |
| `assignment.start/pause/resume/block/submit` | — | own assignments | own assignments |
| `assignment.unblock` | ✓ | tasks they created | own |
| `assignment.approve/request_changes` | ✓ | reviewer rule | ✗ |
| `task.comment` | ✓ | visible tasks | own assigned tasks |

## 6. Screens

- **Task list:** filters for status, assignee, priority, due range, overdue, has_blocked and creator. Sort by due date. 38.2% list / 61.8% detail split.
- **Task detail:**
    - left: overview, assignees with per-person status chips and actions;
    - right tabs: Discussion, Files (M08), History (`task_events`).
- **Create task:**
    - title, description, priority, due date and time, optional start;
    - assignees (multi-select restricted by permission);
    - reference files (M08 picker; stubbed until M08);
    - "Hide from TV" option.
- **Dashboards:**
    - Super Admin: counts by derived status, overdue, waiting-for-input list (with blocked hours), recent submissions, today's assignments, workload per person.
    - Sub-Admin: the same, scoped to their subtree.
    - Employee: Resume today, My tasks by status, Due soon (48 h).

## Steps and prompts

### M07.1 — Models + code sequence + search columns
```
Read CLAUDE.md, docs/plan/01_ARCHITECTURE_CONTRACT.md and docs/plan/M07_TASKS.md. Implement Step M07.1: models tasks, task_assignments, task_comments and task_events (AppendOnly) with exact columns; the task_code yearly sequence; the generated tsvector + GIN; pg_trgm on title; the partial unique index on (task_id, user_id) where removed_at is null; the overdue selector.
Tests: the code sequence across a year boundary; a concurrent create gives unique codes.
```

### M07.2 — Transition table + engine
```
Implement Step M07.2: tasks/state.py, the transition table exactly as in section 2 (as data, not if-chains); services.transition() per section 3; derived-status recomputation; blocked_seconds accounting; the reviewer rule (an assignee can never approve their own work).
Tests:
- a parametrised test over every (from, action) pair: allowed → the expected to-state; disallowed → InvalidTransition;
- tests/failure/test_F_TK_2.py (two concurrent transitions with the same expected_version → one wins, the other gets 409 with current state);
- test_F_TK_3.py (exception after the event write → nothing persisted);
- test_F_TK_4.py.
```

### M07.3 — Task services, permissions, comments
```
Implement Step M07.3: create_task, update_task (diff + deadline_changed), add_assignees, reassign, cancel_task, add_comment; register every policy in section 5; add the scoped manager Task.objects.visible_to(user).
Tests:
- the full permission matrix (parametrised);
- tests/failure/test_F_TK_1.py (create_task POSTed 3 times with the same key → 1 task);
- test_F_TK_5.py (assigning a deactivated user is rejected; deactivating a user with open assignments is rejected with a list);
- test_F_TK_6.py (manager change → the new manager can approve; the old one cannot; a past-period report attributes to the old manager).
```

### M07.4 — End-of-day closure + resume
```
Implement Step M07.4: the EOD closure modal, the single-transaction batch apply (all or nothing) followed by M06 logout END_OF_DAY, and the Resume-today card.
Tests: one invalid choice → nothing applied and errors returned; a valid batch → all transitions plus the session closed END_OF_DAY; idempotent replay.
```

### M07.5 — Screens + dashboards
```
Implement Step M07.5: task list with filters (cursor pagination), task detail (left 38.2 / right 61.8), create task, the three dashboards, and the employee-facing status labels from section 2. Register the M03 timeline provider and the M06 day-drawer provider.
Tests: query-count assertions (list ≤ 8 queries for 50 rows; dashboard ≤ 12); each role sees only permitted tasks.
```

### M07.6 — Delivery receipts
```
Implement Step M07.6 per section 7: the event_receipts model, KEY_EVENTS, receipt rows created in the same transaction as each key event, POST /app/receipts/ack (idempotent, recipient-only, <= 100 ids, server time), seen_at set when the recipient opens the task detail, the three marks on the assignee chip, "Not delivered yet" after 2 minutes, the manager dashboard count and list, and the URGENT 15-minute notification to the sender. Thresholds are settings.
Tests: tests/failure/test_F_TK_7.py (the recipient is offline: the receipt stays pending and shows "Not delivered yet"; after reconnect the client fetches missed changes and acks, and it becomes delivered with the server's time); tests/failure/test_F_TK_8.py (the same ack twice changes nothing; acking someone else's receipt is refused and audited as security.denied; a client time in the body is ignored); a receipt never blocks or changes the task transition; permission cells for who can see receipts; query counts on the dashboard list.
```

## M07 done-gate

- [ ] Every transition and permission cell is tested.
- [ ] F-TK-1 to F-TK-8 are green.
- [ ] A manager sees ✓, ✓✓ and "Not delivered yet" on a real change while the assignee's PC is disconnected, and ✓✓ after it reconnects.
- [ ] A group task shows independent progress per person.
- [ ] End-of-day closure works on a real PC.
