# 21 — Failure Test Catalogue

Every row has a test file `tests/failure/test_<ID with _ instead of ->.py`. A module is not done until its rows are green. Alert keys must exist in `ots/health/alert_catalogue.py`.

**Legend:**

- **Auto:** what the system does without a person.
- **Manual:** what a person does.
- **RB:** runbook in `22_RUNBOOKS.md`.

## Core

| ID | Scenario | Expected (Auto) | Alert key | Manual / RB |
| --- | --- | --- | --- | --- |
| F-CORE-1 | Same request with the same Idempotency-Key sent 3× concurrently | 1 row; 3 identical responses; replays flagged | — | — |
| F-CORE-2 | Worker killed mid-job | The stale `job_runs` row is marked ABORTED at start-up; the job re-runs once, safely | `job.overdue.<name>` if it is late | — |
| F-CORE-3 | Crash between business commit and event dispatch | The event is dispatched after restart; handlers apply once | `outbox.handler_failed` on repeated failure | — |

## Identity and hierarchy

| ID | Scenario | Expected | Alert key | Manual / RB |
| --- | --- | --- | --- | --- |
| F-ID-1 | Duplicate login ID (case-insensitive) or employee ID | Rejected by a unique constraint; friendly error | — | — |
| F-ID-2 | Deactivate or demote the last active Super Admin | Rejected by trigger + service | — | — |
| F-ID-3 | Employee attempts a role escalation through a crafted request | 403; `security.denied` audit | — | Review the audit log |
| F-ID-4 | Repeated wrong passwords | 5 failures in 15 min → locked 15 min (423); auto-unlock | `auth.lockouts` (WARNING if > 3 per day) | Break-glass / `reset_master` |
| F-HR-1 | Manager missing, inactive, lower rank, or a loop | Rejected at DB and service level | — | — |
| F-HR-2 | Manager changed | New `reporting_history` row; past reports use the old manager | — | — |

## Audit

| ID | Scenario | Expected | Alert key |
| --- | --- | --- | --- |
| F-AU-1 | Audit write fails | The business change rolls back | — |
| F-AU-2 | UPDATE or DELETE on an append-only table | Rejected by trigger and privileges | — |

## Tasks

| ID | Scenario | Expected |
| --- | --- | --- |
| F-TK-1 | Double submit or network retry | One effect; replayed response |
| F-TK-2 | Concurrent edit | One wins; the other gets 409 with the current state |
| F-TK-3 | Crash mid-transition | Full rollback |
| F-TK-4 | Illegal transition | 409 InvalidTransition; no change |
| F-TK-5 | Deactivated assignee | Assignment refused; deactivation blocked while work is open |
| F-TK-6 | Manager changed after assignment | The current manager approves; history attributes correctly |
| F-TK-7 | Recipient offline when a key change is made | Change is saved; receipt shows "Not delivered yet"; after reconnect it is fetched, acked and becomes delivered with the server's time |
| F-TK-8 | Duplicate or forged acknowledgement | A repeat changes nothing; acking someone else's receipt is refused and audited; client time is ignored |

## Files and storage

| ID | Scenario | Expected | Alert key | RB |
| --- | --- | --- | --- | --- |
| F-FL-1 | Browser upload interrupted | Resume from `received_bytes` with the same key | — | — |
| F-FL-2 | SHA-256 mismatch at the end of an upload | REJECTED; no records; staging deleted | — | — |
| F-FL-3 | Disk ≥ 90% | Eviction tried; then uploads 507; WAL and DB unaffected | `disk.critical` | Free space |
| F-FL-4 | Remote upload succeeded, DB commit failed | Orphan sweep or 409 adoption; no duplicate | `storage.orphan` (WARNING if quarantined) | — |
| F-FL-5 | Archived item deleted or missing in SharePoint | MISSING; repair from cache | `storage.missing` | D |
| F-FL-6 | Evicted file requested while the internet is down | Info page; fetched and notified later | — | — |
| F-FL-7 | New version vs retry | A retry returns the same version; a new key gives N+1 | — | — |
| F-FL-8 | Blocked extension | Rejected unless the Super Admin overrides (audited) | — | — |
| F-ST-1 | Internet down | Queue holds; files usable on the LAN | `storage.offline` | — |
| F-ST-2 | Graph 429/503 with Retry-After | Whole queue pauses for exactly Retry-After | `storage.throttled` (INFO) | — |
| F-ST-3 | Grant revoked or certificate invalid (401/403) | BLOCKED_AUTH; probe every 15 min | `storage.auth` | C, OPS-5 |
| F-ST-4 | Storage full (507 or ≥ 95%) | BLOCKED_QUOTA for files; DR continues | `storage.quota` | C |
| F-ST-5 | Retries exhausted (24 h) | FAILED dead letter; local copy pinned | `storage.dead_letter` | Retry from the health page |
| F-ST-6 | Upload session expired | Adopt if complete, else a new session | — | — |
| F-ST-7 | 409 name conflict | Hash match → adopt; mismatch → FAILED | `storage.conflict` | — |
| F-ST-8 | Certificate expiring | WARNING at 30 days, CRITICAL at 7 days | `storage.cert_expiring` | OPS-5 |

## Attendance

| ID | Scenario | Expected | Alert key |
| --- | --- | --- | --- |
| F-AT-1 | Browser closed or PC crashed | BROWSER_CLOSED at the disconnect time, after 2 min | — |
| F-AT-2 | Idle code in the browser stalls | The server closes the session IDLE_45 | `attendance.sweep_stale` if the sweep stops |
| F-AT-3 | Camera unavailable | Login blocked; audited | — |
| F-AT-4 | Wrong device clock | Server time used | — |
| F-AT-5 | Server restart | Sessions survive; no false BROWSER_CLOSED | — |
| F-AT-6 | Duplicate login submission | One session | — |
| F-AT-7 | Selfie upload fails | 3 retries, then clean failure; no partial rows | — |

## PWA sync

| ID | Scenario | Expected |
| --- | --- | --- |
| F-SY-1 | Server or LAN unreachable | Allowed actions queue; ordered delivery later, flagged `offline_synced` |
| F-SY-2 | Stale `base_version` | CONFLICT card; no silent merge |
| F-SY-3 | Browser storage near full or evicted | Warning at 80%; offline files refused at 95%; server data unaffected |
| F-SY-4 | Login expires with items pending | Held; resumed only for the same user |
| F-SY-5 | Several tabs | A single sender; no duplicate delivery |

## Live and notifications

| ID | Scenario | Expected | Alert key |
| --- | --- | --- | --- |
| F-TV-1 | TV connection lost or server restart | Reconnect + full reload; polling fallback; nightly reload | — |
| F-LV-1 | Dispatcher lag > 60 s | Alert; clients converge on reconnect | `live.lag` |
| F-LV-2 | Too many SSE connections | Oldest per user evicted; 503 beyond the process cap | — |
| F-NT-1 | Handler retried | One notification | — |

## Backup and database

| ID | Scenario | Expected | Alert key | RB |
| --- | --- | --- | --- | --- |
| F-BK-1 | Nightly job did not start | Watchdog at 23:30 | `backup.nightly_not_started` | Run manually |
| F-BK-2 | Dump, upload or verify fails | FAILED/PARTIAL; retries; previous good set kept | `backup.nightly_failed` | — |
| F-BK-3 | WAL not shipping | Amber at 10 min, red at 15 min | `backup.rpo` | C |
| F-BK-4 | WAL spool filling | WARNING at 6 GB, CRITICAL at 7.5 GB | `backup.spool_size` | — |
| F-BK-5 | Gap in the WAL chain | CRITICAL + immediate base backup | `backup.wal_gap` | — |
| F-BK-6 | Test restore fails | CRITICAL; prior points kept | `backup.test_failed` | — |
| F-BK-7 | Archiver failing inside PostgreSQL | CRITICAL | `backup.archiver_failing` | — |
| F-DB-1 | DB process crash | Docker restart; WAL crash recovery | `db.restarted` (INFO) | — |
| F-DB-2 | Corruption or bad change | — | — | A |
| F-DB-3 | Disk full | Uploads blocked before DB space is touched | `disk.critical` | — |

## Infrastructure

| ID | Scenario | Expected | Alert key | RB |
| --- | --- | --- | --- | --- |
| F-IN-1 | Power cut | Clean UPS shutdown at 20%; auto-start | `power.on_battery` (via NUT hook) | — |
| F-IN-2 | Server destroyed | — | — | B |
| F-IN-3 | Bad release or migration | Automatic rollback when /healthz fails | `deploy.rolled_back` | E |

## Reports, health, security

| ID | Scenario | Expected | Alert key |
| --- | --- | --- | --- |
| F-RP-1 | Huge export | Streamed; bounded memory | — |
| F-RP-2 | Past-period hierarchy | Uses `reporting_history` | — |
| F-HM-1 | Alert acknowledged but still active | Hidden 4 h; not resolved | — |
| F-HM-2 | Job silently not running | Watchdog alert | `job.overdue.<name>` |
| F-SC-1 | Cross-user object access | 403/404 on every route | — |
| F-SC-2 | Path traversal | Rejected | — |
| F-SC-3 | Missing CSRF token | 403 | — |
| F-SC-4 | Selfie past retention | Deleted; session kept; audited | — |
| F-SC-5 | Secret leaking into a backup | Not present in any artefact | — |

## Alert catalogue (copy into `ots/health/alert_catalogue.py`)

```
auth.lockouts WARNING | auth.breakglass_used CRITICAL | auth.reset_master CRITICAL
job.overdue.<name> WARNING | outbox.handler_failed WARNING | outbox.dead_letter CRITICAL
storage.offline WARNING | storage.throttled INFO | storage.auth CRITICAL | storage.quota CRITICAL
storage.dead_letter CRITICAL | storage.conflict CRITICAL | storage.missing CRITICAL | storage.drifted CRITICAL
storage.orphan WARNING | storage.cert_expiring WARNING/CRITICAL | storage.capacity WARNING/CRITICAL
backup.rpo WARNING/CRITICAL | backup.spool_size WARNING/CRITICAL | backup.archiver_failing CRITICAL
backup.nightly_not_started CRITICAL | backup.nightly_failed CRITICAL | backup.base_failed CRITICAL
backup.wal_gap CRITICAL | backup.test_failed CRITICAL | backup.key_test_due WARNING
disk.warning WARNING | disk.critical CRITICAL | memory.app CRITICAL | db.restarted INFO
live.lag WARNING | attendance.sweep_stale CRITICAL | power.on_battery CRITICAL | deploy.rolled_back CRITICAL
```
