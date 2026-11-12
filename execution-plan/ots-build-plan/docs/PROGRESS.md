# OTS build progress log

Newest day first. Source of truth for step status is `docs/plan/25_STEP_CHECKLIST.md`; this file adds the context.

## 2026-11-09 (day 1)

**Done and committed (13 commits in `project code/`):** M00.1 to M00.9 (Foundation) and M01.1 to M01.3 (Design system).
Gates at end of day: lint, typecheck, 115 tests, 4 failure tests (F-CORE-1, F-CORE-2, F-CORE-3, F-IN-3): all green.

| Step | Result |
| --- | --- |
| M00.1 | Repo scaffold, pyproject, Makefile, Django skeleton |
| M00.2 | 4-container Docker stack (caddy, app, worker, db), healthchecks, Caddy TLS, /ca.crt |
| M00.3 | JSON logging, Argon2id parameters, DB roles (ots_app / ots_backup), migrate.sh |
| M00.4 | uuid7, IST clock, typed errors, request-id and error middleware, fail-closed permissions |
| M00.5 | Idempotency layer (F-CORE-1) |
| M00.6 | Event outbox, AppendOnly migration operation, dispatcher (F-CORE-3) |
| M00.7 | Job scheduler and worker, advisory locks, heavy mutex (F-CORE-2) |
| M00.8 | Alerts API, full /healthz |
| M00.9 | install.sh, deploy.sh, rollback.sh, automatic rollback proven (F-IN-3) |
| M01.1 | Tokens, Tailwind build, vendored htmx / Alpine CSP / Lucide / Inter, template lint, contrast tests |
| M01.2 | Base layout, 12 components, /styleguide (browser-verified) |
| M01.3 | Screen shells and 403/404/500 pages |

**Decisions logged today:** D-013 to D-024 (see `docs/decisions.md`). Notable: colour tokens `ink-400` and `warn` darkened to pass AA; Alpine CSP build from day one; jobs run in threads; no TRUNCATE trigger on append-only tables.

**Running now (local Mac):** Docker stack up; https://localhost/styleguide and /shells/* visible (DJANGO_DEBUG=true in `.dev/etc/.env`).

**Environment facts**
- Local only for now; server comes later. The production server must be a NEW dedicated 4 GB machine: the existing aaPanel host (mail.kropbook.com) has about 1.7 GB RAM free and swap half used, so it is not suitable. Subdomain planned: `peoples.works.kropbook.com`.
- Docker binary is not on PATH: `/Applications/Docker.app/Contents/Resources/bin`.
- Ports 8000 and 8765 on the Mac are used by unrelated processes.
- No changes were made on any server.

**Blocked / waiting on the user**
1. (done 2026-11-11) Microsoft 365 values for the Graph gate (guide: `docs/GUIDE_CONNECT_ONEDRIVE.md`): `GRAPH_TENANT_ID`, `GRAPH_CLIENT_ID`, `GRAPH_SITE_ID`, `GRAPH_DRIVE_BUSINESS`, `GRAPH_DRIVE_DR`. Public certificate to upload: `.dev/etc/graph.cer`, thumbprint `B274B3608718EC6438CE36C97A6639965F648E10`.
2. (done 2026-11-11) Colour token changes accepted (D-022, D-039).
3. Decide the new server (not needed until deployment).

**Not yet verified:** `install.sh` on a clean host (needs the server); real Microsoft Graph (needs the values above).

**M02 status (2026-11-11):** M02.1 to M02.5 built and code-reviewed; the review's findings are fixed (D-031 to D-035, 260 tests green). The Graph gate passed live (G1-G9). Browser-checked (desktop, phone width, JavaScript off) and merged into main (local, not pushed). Not yet done: the 3-PC login done-gate.

**M03 status (2026-11-12):** M03.2 built, browser-checked and committed on branch `m03-audit-viewer` (306 tests green, D-040 to D-042); not merged yet.

**M05 status (2026-11-12):** all eight steps (M05.1 to M05.8) are built and committed on branch `m05-storage` (decisions D-044 to D-050; failure tests F-ST-1 to F-ST-8, F-FL-4, F-FL-5 green; `deploy/scripts/graph_live_check.py` passes 15 of 15 on the real tenant). Not yet merged. The M05 done-gate needs the owner: archive a 200 MB file for real, switch the internet off for 30 minutes and watch the queue drain, and delete a file in the SharePoint web page and see it detected and repaired the next day.

**Next:** merge `m05-storage` and `m03-audit-viewer` on the owner's OK, then M04 (backup and disaster recovery) or M06 (attendance).
