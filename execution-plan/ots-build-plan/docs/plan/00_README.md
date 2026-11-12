# 00 — Build Plan: How to Use This Pack

This pack turns Architecture v3.0 (with v2.0 for detail) into steps Claude Code can carry out one at a time. Each module file holds:

- the specification;
- the data model;
- services;
- endpoints and screens;
- failure IDs;
- tests;
- the copy-paste prompts for each step.

## Files

| File | Content |
| --- | --- |
| `/CLAUDE.md` | Repository rules. Place at the repo root |
| `00_README.md` | This file: order, gates, how to prompt |
| `01_ARCHITECTURE_CONTRACT.md` | Frozen decisions, data classes, budgets, schema master list, enums |
| `02_P0_INFRA_AND_TENANT.md` | Server preparation, Microsoft 365 / SharePoint / Entra setup, Graph verification gate (done by people, not Claude) |
| `M00_FOUNDATION.md` | Repo scaffold, Docker, settings, core utilities (ids, clock, errors, idempotency, events, jobs, alerts) |
| `M01_DESIGN_SYSTEM.md` | Tailwind tokens, base layout, component library (rounded, white, Fibonacci) |
| `M02_IDENTITY_HIERARCHY.md` | Users, roles, profiles, reporting tree, login, lockout, break-glass |
| `M03_AUDIT.md` | audit_log, append-only enforcement, audit viewer |
| `M04_BACKUP_DR.md` | WAL archive, spool, shipper, base backup, nightly export, verification, pruning, restore tools |
| `M05_STORAGE_ONEDRIVE.md` | StorageProvider, GraphProvider, operations queue, retries, reconciliation, capacity |
| `M06_ATTENDANCE.md` | Selfie login, work sessions, idle enforcement, attendance days, calendar, corrections |
| `M07_TASKS.md` | Tasks, assignments, state machine, statuses, end-of-day closure, comments, dashboards |
| `M08_FILES.md` | Upload pipeline, file codes, versions, cache, handovers, trail, search |
| `M09_PWA_SYNC.md` | Manifest, service worker, IndexedDB outbox, conflicts |
| `M10_LIVE_TV.md` | SSE, notifications fan-out, TV board, display pairing |
| `M11_HISTORY_REPORTS.md` | Calendars, work history, search, reports |
| `M12_HEALTH_MONITORING.md` | System health page, alerts, metrics, disk guards |
| `M13_SECURITY_HARDENING.md` | Headers, CSP, firewall, TLS, privacy policy hooks, selfie retention |
| `20_INTEGRATION_PLAN.md` | Cross-module wiring, event catalogue, end-to-end flows |
| `21_FAILURE_TEST_CATALOGUE.md` | All 43 failure IDs plus additions, with test recipes |
| `22_RUNBOOKS.md` | Recovery runbooks A to E plus operating procedures |
| `23_RELEASE_QUALIFICATION.md` | Final acceptance tests 1 to 37, load test, go-live checklist |
| `24_PROMPT_LIBRARY.md` | Session-start prompt, step prompt template, review prompts, fix-loop prompts |

## Build order and gates

The order is mandatory. **The backup pipeline (M04) goes live before any business data exists.** Business modules come after it.

| Week | Phase | Steps | Gate (must pass before the next phase) |
| --- | --- | --- | --- |
| 0 (days 0 to 3) | P0 | `02_P0` all tasks | Graph verification gate passed; webcams fitted; Chrome or Edge on every PC; root CA installed |
| 1 | P1 Foundation | M00 → M01 → M02 → M03 | Login works on 3 PCs; tree rules tested; audit append-only proven |
| 1 to 2 | P1 Protection | M05.1 to M05.4 (provider and queue) → M04 (all) | WAL shipping to SharePoint; recovery-point age under 10 min; first logical restore test passes; PITR test passes |
| 2 | P2 Attendance | M06 | All 5 session end reasons proven; at most 10 writes per employee per day |
| 3 to 4 | P3 Tasks | M07 | Every transition and permission cell tested |
| 5 to 6 | P4 Files | M08 + M05.5 to M05.8 (reconciliation, capacity) | 200 MB file archives and verifies; internet-unplug test passes |
| 7 | P5 PWA sync | M09 | Offline queue tests 32 and 33 pass |
| 7 to 8 | P6 Live + TV | M10 | Board updates in 3 s or less; recovers after server restart |
| 8 to 9 | P7 History + Reports + Health | M11, M12, M13 | Search of 10k tasks in under 500 ms; health page complete |
| 10 | P8 Qualification | `23_RELEASE_QUALIFICATION` | Every test 1 to 37 and every failure ID green; Runbook B exercise timed |

## How to drive Claude Code

1. Start every session with the **Session Start Prompt** in `24_PROMPT_LIBRARY.md`.
2. Give it **one step** at a time, using the step prompt printed in the module file.
3. After each step, run the **Review Prompt** (in `24_PROMPT_LIBRARY.md`) in a fresh session, so the code is not graded by the session that wrote it.
4. Commit after each green step, using the format `M07.3: assignment state machine`.
5. Never let a session work on two modules at once.

## Refinements made while writing this plan (logged in the decision log)

1. **Local plaintext WAL archive, 7 days.** Automated restore tests run on the server, and the server must never hold the age private key. So the archive script writes two copies:
    - an encrypted copy to the ship spool;
    - a zstd-compressed plaintext copy to `/srv/data/wal-local` (kept 7 days).

   The latest base backup and latest logical export are also kept locally in plaintext. Automated weekly and monthly restore tests use these local copies. The remote encrypted chain is proven by the quarterly key test and the disaster-recovery exercise. Runbook A (bad change) can then run without internet.
2. **USB disk removed.** OneDrive is the only off-site destination, per the user's decision.
3. **Job scheduler is PostgreSQL-backed.** The `jobs` table, advisory locks, and `SELECT … FOR UPDATE SKIP LOCKED` replace any broker.
