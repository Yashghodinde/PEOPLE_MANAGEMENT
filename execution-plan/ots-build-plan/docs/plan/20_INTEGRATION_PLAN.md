# 20 — Integration Plan

This file is the cross-module wiring: dependencies, the event catalogue, the end-to-end flows and the integration test suite. Run the integration suite after each phase gate.

## 1. Module dependency map

```
M00 Foundation ─┬─> M01 Design ─┬─> M02 Identity ─> M03 Audit ─┬─> M06 Attendance ─┐
                │               │                               ├─> M07 Tasks ──────┼─> M08 Files ─> M09 PWA ─> M11 History/Reports
                │               │                               │                   │
                ├─> M05.1–4 Storage core ─> M04 Backup/DR       └─> M10.1 SSE ──────┘   M10.2–5 Notifications/TV
                └───────────────────────────────────────────────────────────────────────> M12 Health ─> M13 Security
```

**Rules:**

- A module may import only `services` and `selectors` of modules to its left.
- It may never import their models directly for writing.
- Reverse communication happens only through events (section 2) and provider registries (timeline, day drawer, completion handlers).

## 2. Event catalogue (outbox `type` → subscribers)

| Event | Emitted by | Payload | Subscribers |
| --- | --- | --- | --- |
| `user.created` / `user.updated` / `user.deactivated` | M02 | user_id | M10 Hub (refresh audience caches) |
| `user.manager_changed` | M02 | user_id, old, new | M10 Hub, M11 (none; uses history) |
| `session.started` | M06 | session_id, user_id | M10 (team-today widget refresh) |
| `session.ended` | M06 | session_id, user_id, reason | M10 (`logged-out` to that user's connections; team-today refresh) |
| `task.created` | M07 | task_id | M10 (`board-changed`, `task-changed`) |
| `assignment.created` | M07 | assignment_id, task_id, user_id | M10 notifications (assignee), `board-changed` |
| `assignment.started` / `.resumed` / `.paused` | M07 | ids | `board-changed`, `assignment-changed` |
| `assignment.blocked` | M07 | ids, reason | Notifications (reports-to manager + creator), `board-changed` |
| `assignment.unblocked` | M07 | ids | `board-changed` |
| `assignment.submitted` | M07 | ids, late | Notifications (reviewers), `board-changed` |
| `assignment.changes_requested` / `.approved` | M07 | ids | Notifications (assignee), `board-changed` |
| `assignment.removed` / `task.cancelled` | M07 | ids | Notifications (assignee), `board-changed` |
| `task.updated` / `task.deadline_changed` | M07 | task_id, diff | Notifications (assignees, deadline only), `task-changed`, `board-changed` |
| `task.commented` | M07 | task_id, comment_id | Notifications (other participants), `task-changed` |
| `file.staged` | M08 | version_id, task_id | `task-changed` |
| `file.archived` | M08 (via M05 handler) | version_id | `file-archived` (badge update) |
| `file.missing` / `file.drifted` | M05 | version_id | Alerts (already raised), `file-archived` (badge shows warning) |
| `file.handed_over` | M08 | handover_id | Notifications (recipients) |
| `file.handover_opened` | M08 | handover_id, user_id | Notifications (sender) |
| `alert.raised` / `alert.resolved` | M00.8 | key, severity | M10 (`alert` to Super Admins), notifications (CRITICAL) |
| `backup.run_finished` | M04 | run_id, status | Health card refresh |

Every subscriber is idempotent on `event_id`, and every subscriber has a test.

## 3. Provider registries

| Registry | Owner | Providers |
| --- | --- | --- |
| Activity timeline sources | M03 | M06 sessions, M07 task events, M08 uploads and handovers, M03 auth audit |
| Attendance day-drawer "Work" block | M06 | M07 (tasks touched), M08 (uploads) |
| Storage completion handlers | M05 | `ARCHIVE_FILE` → M08 `on_archived`; `SHIP_WAL` → M04 `on_wal_shipped`; `UPLOAD_BACKUP_OBJECT` → M04 `on_backup_object`; `REPAIR_FILE` → M08 `on_repaired` |
| Health cards | M12 | M04, M05, M06, M10, M00 jobs |
| Alert catalogue | M12 | All modules |

## 4. End-to-end flows (each is an integration test in `tests/integration/`)

### E2E-1 Assign → work → submit → approve → archive

1. The Super Admin creates task T for Employee E with a reference file.
2. Expected: E gets a notification; the TV shows a card ≤ 3 s; `task_events` has `task.created` + `assignment.created`.
3. E starts the task. The TV shows "Working".
4. E uploads a submission (12 MB). The version is STAGED, the badge says "Not yet off-site", and an `ARCHIVE_FILE` operation is enqueued.
5. The worker archives it. The version is ARCHIVED and the badge clears live.
6. E submits. The reviewer is notified and the TV shows "Completed (awaiting review)".
7. The Super Admin approves. Derived status is COMPLETED and the card leaves the TV.
8. **Assertions:**
    - timestamps are in order;
    - every state change has exactly one event;
    - `audit_log` has `file.download` for every download done during the test;
    - the manifest line exists.

### E2E-2 Internet outage during the workday

1. Block Graph (fake provider in outage mode, or iptables on staging).
2. For 60 min: uploads, transitions and logins all work.
3. RPO alert: amber at 10 min, red at 15 min. The health page shows the exposure.
4. Restore Graph. WAL ships first (in segment order), then backup objects, then files. All verify.
5. RPO returns under 10 min and the alerts resolve.

### E2E-3 Server down while employees work

1. Stop `app` for 5 min.
2. Employees change status and comment in the PWA. The actions queue in the outbox and the pending chip shows the count.
3. Start `app`.
4. The outbox delivers in order with no duplicates, flagged `offline_synced`. Sessions survive (F-AT-5).

### E2E-4 Idle and session lifecycle

1. Log in with a selfie.
2. Idle for 46 min (CDP override). The session closes IDLE_45 with the backdated time.
3. Log in again; a new session opens.
4. Close the browser. The session closes BROWSER_CLOSED after 2 min.
5. The attendance day shows 2 sessions and correct active minutes.

### E2E-5 Disaster recovery (staging machine)

1. Seed 1 day of activity, shipping to a test DR library.
2. Destroy the database volume.
3. Run Runbook B with the test age key.
4. Assertions: data matches up to the last shipped WAL, and files download from Business-Files.

### E2E-6 Manager change mid-period

1. Move E from manager M1 to M2 on the 15th.
2. Assertions:
    - M2 can approve E's open work;
    - M1 cannot;
    - the completion report for the 1st to the 14th attributes E to M1.

## 5. Integration prompts

**Prompt INT-1 (after P3)**
```
Read CLAUDE.md and docs/plan/20_INTEGRATION_PLAN.md. Implement tests/integration/test_e2e_1_assign_to_archive.py and test_e2e_6_manager_change.py exactly per section 4, using the fake storage provider and a real worker loop run in-process (call the job functions directly in order). Fix any wiring defect found. Do not change the specifications. If a spec conflict appears, stop and report it.
```

**Prompt INT-2 (after P4)**
```
Implement tests/integration/test_e2e_2_internet_outage.py and test_e2e_4_idle_lifecycle.py (the idle part uses freezegun plus direct calls to the idle endpoints; the Playwright variant lives in tests/e2e). Verify the ship order: WAL before backups before files.
```

**Prompt INT-3 (after P5)**
```
Implement tests/e2e/test_e2e_3_server_down.py with Playwright: stop the app container via the docker SDK, perform actions, restart, and assert ordered, non-duplicated delivery and offline_synced flags.
```

**Prompt INT-4 (P8)**
```
Implement deploy/scripts/dr_exercise.sh, which automates E2E-5 on a staging host. It uses the test age key from tests/keys/ (never the production key) and outputs a timed report in Markdown for docs/qualification/.
```

## 6. Event-coverage check

A test (`tests/integration/test_event_coverage.py`) does the following:

- parses every `events.publish('<type>'…)` call in the codebase;
- asserts each type is in the catalogue in section 2 (maintained as `ots/core/event_catalogue.py`);
- asserts each catalogued type has at least one subscriber or is marked `audit_only=True`.
