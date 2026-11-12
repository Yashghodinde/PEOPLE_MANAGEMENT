# CLAUDE.md: Office Task System (OTS)

> Place this file at the repository root. Claude Code reads it at the start of every session.
> Authoritative architecture: `docs/plan/01_ARCHITECTURE_CONTRACT.md`. If the code and the contract disagree, the contract wins. If you believe the contract is wrong, stop and ask. Do not improvise.

## What this system is

OTS is a LAN-hosted office platform with these parts:

- Task management.
- A file repository: every file version is archived permanently to a SharePoint site in the company's Microsoft 365 tenant.
- Attendance and work sessions: selfie at login, 45-minute idle logout.
- A read-only TV board.
- Disaster recovery with a 15-minute recovery point (PostgreSQL WAL shipped to SharePoint).

It runs on one Linux server (2 cores, 4 GB RAM, 80 GB disk). Today it serves 7 to 9 users, with headroom for 30 and a scaling path beyond that.

## Stack (fixed: do not substitute)

| Layer | Choice | Version |
| --- | --- | --- |
| Language | Python | 3.12 |
| Web framework | Django | 5.2 LTS |
| ASGI server | Uvicorn | latest 0.3x, **1 worker** |
| Database | PostgreSQL | 16 |
| DB driver | psycopg | 3.x (binary) |
| UI | Django templates + HTMX 2 + Alpine.js 3 + Tailwind CSS (compiled at build) | vendored, no CDN |
| Calendar | FullCalendar 6 | vendored, loaded only on calendar pages |
| Proxy / TLS | Caddy 2 | `tls internal` |
| Containers | Docker Engine + Compose v2 | |
| Microsoft Graph auth | `msal` (certificate client credentials) | latest |
| HTTP client | `httpx` | latest |
| Password hashing | Argon2id via `argon2-cffi` | m=19456 KiB, t=2, p=1 |
| Compression / encryption (backups) | `zstd` CLI, `age` CLI | distro packages |
| Tests | `pytest`, `pytest-django`, `pytest-asyncio`, `factory-boy`, `freezegun`, `respx` | |
| Lint / format | `ruff` (lint + format), `mypy` (strict on `ots/core`, `ots/storage`, `ots/backup`) | |

**Forbidden without a written decision-log entry:** React or any SPA framework, Redis, Celery, Kafka, Elasticsearch, Nginx, microservices, ORMs other than Django's, client-side CDNs, external fonts, and storing files inside PostgreSQL.

## Repository layout

```
/CLAUDE.md
/docs/plan/                 build plan (these files)
/docs/decisions.md          architecture decision log (append-only)
/deploy/
  compose.yaml
  .env.example
  caddy/Caddyfile
  postgres/Dockerfile       postgres:16 + age + zstd
  postgres/postgresql.conf
  postgres/ots-wal-archive.sh
  scripts/                  install.sh, deploy.sh, rollback.sh, install-root-ca.ps1, install-root-ca.sh
/app/
  manage.py
  config/settings/{base.py,prod.py,test.py}
  config/{urls.py,asgi.py}
  ots/core/                 ids, clock, idempotency, permissions, events, jobs, alerts, middleware, health
  ots/accounts/             users, auth, profiles, hierarchy, reporting history
  ots/audit/                audit_log, task_events helpers, append-only triggers
  ots/attendance/           work sessions, selfies, idle enforcement, attendance days, corrections, holidays
  ots/tasks/                tasks, assignments, state machine, comments, EOD closure
  ots/files/                files, versions, blobs, file codes, handovers, access log, cache
  ots/storage/              StorageProvider, GraphProvider, operations queue, reconciliation, capacity
  ots/sync/                 idempotency records, outbox protocol endpoints
  ots/live/                 SSE stream, TV board, display pairing
  ots/notifications/        in-app notifications
  ots/history/              calendars, search, work history
  ots/reports/              workload, blocked time, attendance reports
  ots/backup/               WAL shipper, base backup, logical export, pruning, verification, restore tools
  ots/health/               System health page, metrics
  templates/  static/  static_src/
/tests/                     mirrors ots/ packages + /tests/failure/ (one file per failure ID)
```

## Non-negotiable engineering rules

1. **Server time only.** Every event timestamp comes from `ots.core.clock.now()`, which wraps `django.utils.timezone.now()`. It is stored as `timestamptz` in UTC and displayed in `Asia/Kolkata`. Never accept a timestamp from a client as truth. Store client time only in a field named `client_observed_at`.
2. **Identity is `user_id` (UUID).** Never use IP address or device as identity. IP is logged in `audit_log.client_ip` for forensics only.
3. **Atomic business change + audit.** Every state change goes through a `services.py` function wrapped in `transaction.atomic()`, and that function writes the change and its event row. Views never write models directly.
4. **Append-only history.** `task_events`, `audit_log`, `file_access_log` and `outbox_events` reject UPDATE and DELETE through database triggers, and the app's database role lacks UPDATE and DELETE on them. Corrections are new rows.
5. **No cascade deletes** on any foreign key to `users`, `tasks`, `files` or `file_versions`. Use `on_delete=PROTECT`. Users are deactivated, never deleted.
6. **Idempotency.** Every POST, PUT or PATCH that changes data requires an `Idempotency-Key` header (UUIDv7 from the client). The middleware in `ots.core.idempotency` replays the stored response for a repeated key.
7. **Optimistic locking.** Mutable aggregates (`tasks`, `task_assignments`, `users`) have an integer `version` column. Updates use `WHERE version = :expected`. A mismatch returns HTTP 409 with the current state.
8. **Permissions on the server.** Every view calls `ots.core.permissions.require(user, action, obj)`. Every queryset starts from a scoped manager, for example `Task.objects.visible_to(user)`. Hiding a template element is cosmetic only.
9. **No false success.** The UI shows "Saved" only after commit. A file shows "Off-site" only after state `ARCHIVED`. A backup is `SUCCESS` only after remote checksum verification.
10. **Only `ots.storage` talks to Microsoft.** No other package imports `msal`, `httpx` against `graph.microsoft.com`, or Graph URLs.
11. **Nothing is batched on a timer** for business data. Client actions are sent immediately. Periodic work belongs to the worker's job scheduler only.
12. **Memory discipline.** Never read a whole file into memory: stream in 1 MiB chunks. Never load unbounded querysets: paginate (default 50, maximum 200). No N+1 queries: tests assert query counts on list views.
13. **Secrets.** Secrets live in `/etc/ots/.env` (root-only, mode 600), the Graph certificate private key in `/etc/ots/graph.key`, and the age public key in `/etc/ots/backup.pub`. **The age private key must never exist on the server.**
14. **Every failure has a state.** When you implement a module, implement every failure ID listed in its plan file. Each ID has a test in `tests/failure/test_<ID>.py`.

## Coding conventions

- Settings via `django-environ`. No hardcoded hosts or paths: all paths come from settings (`OTS_DATA_DIR=/srv/data`, etc.).
- Models: `id = UUIDField(primary_key=True, default=uuid7)` from `ots.core.ids`. Business codes (`EMP001`, `T-2026-000123`) are separate unique fields.
- Use `TextChoices` for enums. Persist the enum values exactly as written in the plan files.
- Module layout: `models.py`, `services.py` (all writes), `selectors.py` (all reads), `views.py` (thin), `permissions.py`, `urls.py`, `templates/<app>/`, `tests/`.
- Services raise typed exceptions from `ots.core.errors` (`PermissionDenied`, `Conflict`, `InvalidTransition`, `ValidationFailed`, `QuotaBlocked`, `StorageUnavailable`). A single middleware maps them to HTTP responses.
- Every service that changes state publishes an event through `ots.core.events.publish(...)` inside the same transaction.
- Logging: JSON lines to stdout with `request_id`, `user_id` and `event`. No PII in logs beyond `user_id`.
- Migrations: forward-only in production. Every migration that alters or drops a column requires a pre-migration logical dump in `deploy.sh`.

## How to work in this repo

1. Read `docs/plan/00_README.md` for the build order. Work on **one step** at a time, as named in the module file (for example "Step M06.3").
2. Before coding, restate the step's acceptance criteria. After coding, run:
   ```
   make lint && make typecheck && make test
   ```
   All must pass. Do not mark a step done with failing or skipped tests.
3. Every step ends with:
   - the tests named in the step, passing;
   - the failure tests named in the step, passing;
   - a one-line entry in `docs/CHANGELOG.md`;
   - an entry in `docs/decisions.md` if anything deviated from the plan, with the reason.
4. If a step depends on a step that is not complete, stop and say so.
5. If an external system (Microsoft Graph, the TV) is unavailable in your environment, implement against the provider interface and test with `respx` mocks. Mark the live verification as a manual checklist item in the step's PR description.

## Commands

```
make up            # docker compose up -d (dev)
make test          # pytest -q
make lint          # ruff check . && ruff format --check .
make typecheck     # mypy app/ots/core app/ots/storage app/ots/backup
make migrate       # manage.py migrate
make worker        # manage.py run_worker (foreground)
make e2e-failure   # pytest tests/failure -q
```
