# M12 — System Health, Alerts, Disk Guards

**Depends on:** M00.8 (alerts), M04, M05, M06, M10.
**Produces:**

- the System health page (Super Admin);
- the alert banner;
- disk and memory guards;
- job status;
- alert catalogue enforcement.

**Failure IDs:**

- F-DB-3: disk full.
- F-HM-1: an alert that is never resolved stays visible.
- F-HM-2: a silent job (missed schedule) raises an alert.

## 1. Health cards (green / amber / red), one per module

| Card | Shows | Amber | Red |
| --- | --- | --- | --- |
| Backup | Recovery-point age, last WAL shipped, spool size, last nightly status and step, last base backup, last logical and PITR test | RPO 10 to 15 min; spool > 6 GB; last test > 8 days | RPO > 15 min; nightly not SUCCESS; test failed |
| Storage | Queue length by kind, oldest pending, blocks (AUTH/QUOTA), tenant usage %, certificate days left, last delta run | Queue oldest > 1 h; usage ≥ 70%; certificate ≤ 30 days | Any block; usage ≥ 90%; certificate ≤ 7 days; FAILED > 0 |
| Disk | Total %, per-directory sizes vs caps (contract section 4) | ≥ 70% | ≥ 85% |
| Memory | Per container RSS (cgroup files) vs limit | ≥ 80% | ≥ 95% |
| Database | Connections, `pg_wal` size, archiver failed count, longest transaction | `pg_wal` > 2 GB | archiver failing; `pg_wal` > 4 GB |
| Jobs | Every job: last run, status, next run | Overdue by > 30 min | Failed twice in a row |
| Live | SSE connections, dispatcher lag | Lag > 30 s | Lag > 60 s |
| Attendance | Sessions open, TAB_ONLY count today, idle sweep last run | Sweep > 3 min ago | Sweep > 10 min ago |
| Security | Failed logins in 24 h, lockouts, break-glass use, displays paired | Lockouts > 3 | Break-glass used |

- The page polls `/health/cards` every 30 s, plus SSE `alert` events. It is cheap: one query per card, with values cached for 15 s.

## 2. Alerts

- **Banner:** for the Super Admin on every page, showing unresolved CRITICAL alerts with their duration ("Backup to OneDrive failing for 2 h 14 min — step: verify").
- **Alert list:** filter by severity and open/resolved. Acknowledging hides an alert from the banner for 4 h but does not resolve it (F-HM-1). Only the condition clearing resolves an alert.
- **Catalogue enforcement:** `raise_alert` keys must exist in `ots/health/alert_catalogue.py` (key, severity, title template, runbook link). A test asserts every key used in code exists in the catalogue, and that the catalogue matches `21_FAILURE_TEST_CATALOGUE.md`.

## 3. Guards

- **`health.disk_check`** (every 5 min):
    - ≥ 85%: trigger `files.cache_evict` now.
    - ≥ 90%: set the flag `uploads_blocked` (M08 admission returns 507) and raise CRITICAL `disk.critical`.
    - Below 80%: clear the flag.
- **Memory:** if `app` RSS ≥ 95% of its limit for 5 min, raise CRITICAL `memory.app`. Docker restarts the container on an OOM kill; the restart count is shown on the card.

## Steps and prompts

### M12.1 — Metrics collectors + cards
```
Read CLAUDE.md and docs/plan/M12_HEALTH_MONITORING.md. Implement Step M12.1:
- collectors for each card (cgroup v2 memory files, du with caching for directory sizes, pg_stat_archiver, pg_stat_activity, jobs table, Hub stats via an internal endpoint);
- /health page + /health/cards fragment with thresholds exactly per the table.
Tests: threshold → colour mapping per card (table-driven).
```

### M12.2 — Alert catalogue, banner, acknowledgement
```
Implement Step M12.2: alert_catalogue.py with every key from docs/plan/21_FAILURE_TEST_CATALOGUE.md; enforcement tests; the banner; the alert list; the 4 h acknowledgement.
Tests: tests/failure/test_F_HM_1.py (acknowledgement hides the alert but does not resolve it); test_F_HM_2.py (a job with next_run_at 31 min in the past → watchdog alert).
```

### M12.3 — Disk and memory guards
```
Implement Step M12.3: health.disk_check with the eviction trigger, the uploads_blocked flag and the clear; and the memory alert.
Test: tests/failure/test_F_DB_3.py (simulate 91% usage via an injectable statvfs → uploads 507; WAL archiving unaffected; database writes continue).
```

## M12 done-gate

- [ ] Every alert key is in the catalogue.
- [ ] Every card renders against live data on the server.
