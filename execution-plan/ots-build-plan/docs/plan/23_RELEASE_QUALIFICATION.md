# 23 — Release Qualification (P8) and Go-Live

Release 1.0 ships only when every item below is green on the **production server with the production tenant**. Record evidence (screenshots, logs, reports) in `docs/qualification/`.

## 1. Acceptance tests (v1 + v2 + v3)

| # | Test | Pass condition | Evidence |
| --- | --- | --- | --- |
| 1 | Internet unplugged at the router | All LAN functions work; no asset request leaves the LAN (check the browser network tab) | Screenshot |
| 2 | Same employee on 3 PCs | Same identity, history and files | Notes |
| 3 | Group task with 3 assignees | Independent statuses; derived status correct | Test log |
| 4 | Employee requests another employee's task or file URL | 403/404; `security.denied` audited | Audit export |
| 5 | Re-upload a submission | v2 created; v1 still downloads with the same SHA-256 | Hashes |
| 6 | Attach a 6-month-old file to a new task (seeded) | No new blob; the original trail is unchanged | DB query |
| 7 | UPDATE/DELETE on `task_events` | Rejected | psql log |
| 8 | Load: 30 concurrent simulated sessions for 15 min (Locust script `tests/load/locustfile.py`) | p95 page < 500 ms; server RAM < 3.2 GB; no errors | Locust report |
| 9 | Assign a task with the TV on | Card ≤ 3 s | Video |
| 10 | Restart the server with the TV on | Board recovers ≤ 60 s | Video |
| 11 | Pull mains power with the UPS | Clean shutdown; no DB corruption on restart | NUT log |
| 12 | Restore the latest backup on a spare machine | Runbook B completes; integrity PASS | DR report |
| 13 | Fill the disk to 70% (test volume) | Alert shown | Screenshot |
| 14 | 6 wrong passwords | Locked 15 min; audited | Audit export |
| 15 | Create an employee whose manager does not exist | Rejected | Screenshot |
| 16 | Manager change, then last month's report | Old manager shown | Report |
| 17 | Login with the camera blocked | Refused with a message | Screenshot |
| 18 | PC untouched 45 min with Excel in the foreground | IDLE_45, backdated | Session row |
| 19 | Type in Excel 60 min, browser in the background | Session stays open | Session row |
| 20 | Close the browser | BROWSER_CLOSED ≤ 2 min | Session row |
| 21 | Writes per employee per day | ≤ 10 (excluding task actions) | Test output |
| 22 | Upload while offline, reconnect | Archives; hash matches | Op log |
| 23 | Cache past 25 GB | Oldest ARCHIVED evicted; pending kept | Job log |
| 24 | Download an evicted file | Source ONEDRIVE logged | Access log |
| 25 | Handover opened | Trail shows sent and opened | Screenshot |
| 26 | RAM at 8 users for 1 h | < 1.5 GB | Health card |
| 27 | App credential reads another site | Access denied | Gate report |
| 28 | Internet unplugged 2 h during work | LAN work continues; RPO red at 15 min; ordered drain after reconnect | Health history |
| 29 | Kill the worker between upload and commit | Adopted; no duplicate | Op log |
| 30 | Delete an archived file in the SharePoint UI | MISSING the next day; repaired | Alert + op log |
| 31 | PITR to a chosen minute | Canary present / absent as expected | recovery_tests row |
| 32 | Server stopped; PWA actions; restart | Delivered once, in order, flagged `offline_synced` | Event rows |
| 33 | Same event sent 3× | One record | Test output |
| 34 | Browser idle code blocked; away 46 min | Server closes the session IDLE_45 | Session row |
| 35 | WAL spool at 75% | Alert; DB unaffected | Health card |
| 36 | Corrupt one byte of an uploaded backup object | Verify fails; not SUCCESS | backup_runs |
| 37 | Restore with the sealed key only | Succeeds; the server never held the private key | DR report |

## 2. Suites that must be green

- `make lint`, `make typecheck`, `make test`.
- `make e2e-failure`: every ID in `21_FAILURE_TEST_CATALOGUE.md`.
- `tests/integration` (E2E-1 to E2E-6), `tests/security`, `tests/e2e` (Playwright).
- The `@slow` performance tests (search, export, upload memory).

## 3. Load test profile (`tests/load/locustfile.py`)

- 30 users. Each user:
    - every 20 to 60 s: load a dashboard;
    - every 2 min: a task list;
    - every 5 min: a status change;
    - every 10 min: a comment;
    - every 20 min: a 5 MB upload.
- 1 display connected. All 30 users have SSE open.
- Pass criteria: p95 < 500 ms for pages, < 2 s for 5 MB uploads on the LAN; error rate 0; RAM < 3.2 GB; CPU load average < 2.5.

## 4. Go-live checklist

- [ ] P0 complete; the Graph gate report is filed.
- [ ] Sealed key envelope + break-glass credentials + config sheet are stored in two physical locations.
- [ ] Root CA installed and the PWA auto-starts on every PC.
- [ ] The TV is paired and passes the M10.5 checklist.
- [ ] The privacy policy is published and acknowledged by all staff (M13.3).
- [ ] Holidays, shifts and weekly offs are configured.
- [ ] Accounts created in order: managers first, then employees. Reference photos uploaded.
- [ ] 7 days of green health with WAL shipping before go-live.
- [ ] 5-day pilot with 3 to 5 staff: no data-loss or permission defect (otherwise extend by 1 week).
- [ ] Runbooks printed with the key envelope.
- [ ] `docs/decisions.md` reviewed and signed off by the Super Admin.
