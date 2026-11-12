# 25 — Master Step Checklist

Tick each step after its step prompt, review prompt and fix loop are complete and the tests are green. Do the steps in the order listed (it follows `00_README.md`).

## P0 — Infrastructure (people)

- [ ] P0.1 to P0.16 (see `02_P0_INFRA_AND_TENANT.md`)
- [ ] Scripts P0-A, P0-B, P0-C, P0-D written by Claude Code
- [ ] Graph gate G1 to G9: PASS

## P1 — Foundation

- [x] M00.1 Repo scaffold
- [x] M00.2 Containers
- [x] M00.3 Settings, logging, DB roles
- [x] M00.4 Core primitives
- [x] M00.5 Idempotency (F-CORE-1)
- [x] M00.6 Outbox + dispatcher (F-CORE-3)
- [x] M00.7 Jobs + worker (F-CORE-2)
- [x] M00.8 Alerts + healthz
- [x] M00.9 Deploy/rollback (F-IN-3)
- [x] M01.1 Tokens
- [x] M01.2 Base layout + components
- [x] M01.3 Screen shells
- [ ] M02.1 Models + triggers (F-ID-1, F-ID-2, F-HR-1)
- [ ] M02.2 Services + selectors (F-HR-2)
- [ ] M02.3 Login, lockout, break-glass (F-ID-4)
- [ ] M02.4 Policies (F-ID-3)
- [ ] M02.5 Screens
- [ ] M03.1 Audit table (F-AU-1, F-AU-2)
- [ ] M03.2 Timeline + viewer

## P1 — Protection (before any business data)

- [ ] M05.1 Provider interface + fake
- [ ] M05.2 GraphProvider
- [ ] M05.3 Error classifier (F-ST-1 to F-ST-4)
- [ ] M05.4 Operations queue (F-ST-5 to F-ST-7)
- [ ] M04.1 WAL archive script
- [ ] M04.2 Shipper + RPO monitor (F-BK-3, F-BK-4)
- [ ] M04.3 Base backup
- [ ] M04.4 Nightly logical (F-BK-1, F-BK-2)
- [ ] M04.5 Verification + canary (F-BK-5, F-BK-6)
- [ ] M04.6 Pruner
- [ ] M04.7 Restore tooling
- [ ] M04.8 Health card
- [ ] **Gate:** 24 h of RPO under 10 min on production; key test done once

## P2 — Attendance

- [ ] M10.1 SSE + Hub (F-LV-1, F-LV-2)
- [ ] M06.1 Selfie login (F-AT-3, F-AT-6, F-AT-7)
- [ ] M06.2 Idle + sweep (F-AT-1, F-AT-2, F-AT-4, F-AT-5)
- [ ] M06.3 Client idle module
- [ ] M06.4 Roll-up, corrections, holidays
- [ ] M06.5 Calendar + drawer
- [ ] M06.6 Write budget

## P3 — Tasks

- [ ] M07.1 Models
- [ ] M07.2 State machine (F-TK-2, F-TK-3, F-TK-4)
- [ ] M07.3 Services + permissions (F-TK-1, F-TK-5, F-TK-6)
- [ ] M07.4 End-of-day closure
- [ ] M07.5 Screens + dashboards
- [ ] INT-1

## P4 — Files

- [ ] M08.1 Models + codes (F-FL-8)
- [ ] M08.2 Upload pipeline (F-FL-1, F-FL-2, F-FL-3, F-FL-7)
- [ ] M08.3 Client uploader
- [ ] M08.4 Archive, cache, download (F-FL-6)
- [ ] M08.5 Handovers, trail, library, search
- [ ] M05.5 Delta reconcile (F-FL-5)
- [ ] M05.6 Orphan sweep (F-FL-4)
- [ ] M05.7 Capacity + certificate (F-ST-8)
- [ ] M05.8 Inventory + manifest
- [ ] INT-2

## P5 — PWA

- [ ] M09.1 Manifest + service worker
- [ ] M09.2 Outbox (F-SY-1, F-SY-2, F-SY-4, F-SY-5)
- [ ] M09.3 Offline actions (F-SY-3)
- [ ] M09.4 Drafts
- [ ] INT-3

## P6 — Live + TV

- [ ] M10.2 Notifications (F-NT-1)
- [ ] M10.3 HTMX live wiring
- [ ] M10.4 Display + board (F-TV-1)
- [ ] M10.5 TV checklist

## P7 — History, health, security

- [ ] M11.1 to M11.4 (F-RP-1, F-RP-2)
- [ ] M12.1 to M12.3 (F-HM-1, F-HM-2, F-DB-3)
- [ ] M13.1 to M13.3 (F-SC-1 to F-SC-5)

## P8 — Qualification

- [ ] INT-4 DR exercise
- [ ] Acceptance tests 1 to 37
- [ ] Load test
- [ ] Go-live checklist
