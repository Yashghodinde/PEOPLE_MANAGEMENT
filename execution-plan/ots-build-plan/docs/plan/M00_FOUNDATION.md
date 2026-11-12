# M00 — Foundation (repo, containers, core utilities)

**Depends on:** P0.1 to P0.3 (for a real deployment). Development can start immediately on any Linux or macOS machine with Docker.
**Produces:** a running 4-container stack, core utilities used by every module, the job scheduler, the event outbox, the idempotency layer, the alerts API, and deploy/rollback scripts.
**Failure IDs owned:**

- **F-DB-1:** a crashed process is restarted.
- **F-IN-3:** a bad release is rolled back.
- **F-CORE-1:** idempotent replay.
- **F-CORE-2:** worker crash mid-job.
- **F-CORE-3:** event dispatch after crash.

---

## Step M00.1 — Repository scaffold and tooling

**Specification**

- Use the layout from `CLAUDE.md`.
- `pyproject.toml` with these dependencies:
  - Runtime: `django>=5.2,<5.3`, `psycopg[binary]>=3.2`, `uvicorn[standard]`, `django-environ`, `django-htmx`, `argon2-cffi`, `msal`, `httpx`, `croniter`, `uuid6` (for uuid7), `zstandard`, `python-json-logger`, `cryptography`, `whitenoise`.
  - Dev: `pytest`, `pytest-django`, `pytest-asyncio`, `factory-boy`, `freezegun`, `respx`, `ruff`, `mypy`, `django-stubs`.
- `Makefile` with the targets in `CLAUDE.md`.
- `ruff` config: line length 110, rules `E,F,I,B,UP,DJ,S` (bandit subset).
- `.pre-commit-config.yaml` running ruff.
- `docs/decisions.md` seeded with the three refinements listed in `00_README.md`.
- `docs/CHANGELOG.md`.

**Acceptance:** `make lint`, `make typecheck` and `make test` run with 0 tests and exit 0.

**Prompt**
```
Read CLAUDE.md, docs/plan/00_README.md, docs/plan/01_ARCHITECTURE_CONTRACT.md and docs/plan/M00_FOUNDATION.md.
Implement Step M00.1 only.
Create the repository scaffold exactly as in CLAUDE.md "Repository layout": empty Django project `config`, package `ots` with empty subpackages for every module listed.
Add pyproject.toml, Makefile, ruff, mypy and pytest config, pre-commit, docs/decisions.md (seeded with the three refinements from 00_README.md), and docs/CHANGELOG.md.
Acceptance: make lint && make typecheck && make test all exit 0.
Do not create any models yet.
```

## Step M00.2 — Containers

**Specification**

- `app/Dockerfile`, multi-stage:
  1. Stage `assets`: `node:20-alpine`. Builds Tailwind from `static_src/` into `static/css/app.css`. Copies the vendored htmx, alpine and fullcalendar from `static_src/vendor`.
  2. Stage `runtime`: `python:3.12-slim`. Non-root user `ots` (uid 1000). Installs the package; `collectstatic` at build.
  3. Entrypoint is the `app` command, or `worker`.
  4. No compilers in the final image.
- `deploy/postgres/Dockerfile`: `FROM postgres:16`, then `apt-get install -y age zstd`. Copy in `postgresql.conf` and `ots-wal-archive.sh` (a stub that exits 0 until M04).
- `deploy/postgres/postgresql.conf`: the values in contract section 4. `archive_mode=on` and `archive_command='/usr/local/bin/ots-wal-archive.sh %p %f'`.
- `deploy/compose.yaml`:
  - Services `caddy`, `app`, `worker`, `db`, with the memory limits from contract section 3.
  - `restart: unless-stopped`.
  - Healthchecks:
    - `app`: `GET /healthz` every 30 s, timeout 5 s, 3 retries.
    - `db`: `pg_isready` every 10 s.
    - `worker`: the file `/tmp/worker-heartbeat` is touched every 10 s; healthy if it is less than 60 s old.
  - Bind mounts per contract section 3. Only `caddy` publishes 80 and 443.
  - Logging driver `json-file` with `max-size=50m`, `max-file=10`.
- `deploy/caddy/Caddyfile`:
  - Site address `{$OTS_HOSTNAME}`, plus the IP. `tls internal`.
  - `encode zstd gzip`.
  - `request_body max_size 210MB`.
  - Reverse proxy to `app:8000` with `flush_interval -1` (needed for SSE).
  - On port 80: serve `/ca.crt` (Caddy's root certificate) and redirect everything else to https.
- `.env.example` with every key from contract section 8.

**Acceptance:** `docker compose up -d` brings all 4 services to healthy. `curl -k https://localhost/healthz` returns `{"db":"ok","disk":"ok"}`. Killing the `app` process (`docker kill --signal=KILL`) restarts it within 30 s.

**Prompt**
```
Implement Step M00.2 from docs/plan/M00_FOUNDATION.md.
Build the app Dockerfile (multi-stage: assets, then runtime, non-root), deploy/postgres/Dockerfile, postgresql.conf, a stub ots-wal-archive.sh, compose.yaml, Caddyfile and .env.example. All values must match docs/plan/01_ARCHITECTURE_CONTRACT.md sections 3, 4 and 8 exactly.
Add a minimal /healthz view, which checks DB connectivity with SELECT 1 and disk free above 10% on OTS_DATA_DIR.
Write tests/foundation/test_compose_config.py: it parses compose.yaml and asserts memory limits, healthchecks, published ports (only caddy 80/443) and the restart policy.
Acceptance criteria are listed in the step. Report the commands you ran and their outputs.
```

## Step M00.3 — Settings, logging, database roles

**Specification**

- Settings files `base.py`, `prod.py` and `test.py`:
  - `TIME_ZONE = 'Asia/Kolkata'`, `USE_TZ = True`.
  - `DATABASES` from `DATABASE_URL`, with `CONN_MAX_AGE = 60` and `CONN_HEALTH_CHECKS = True`.
  - Password hashers: `Argon2PasswordHasher` subclass `OtsArgon2Hasher` with `memory_cost = 19456`, `time_cost = 2`, `parallelism = 1`.
- Logging: a JSON formatter to stdout with fields `ts`, `level`, `logger`, `request_id`, `user_id`, `event`, `msg`.
- Migration `core/0001_roles.py` (raw SQL, run with migrator credentials):
  - creates roles `ots_app` and `ots_backup` if missing;
  - default privileges as in contract section 9.
- `deploy/scripts/migrate.sh` runs `manage.py migrate` using `DATABASE_MIGRATOR_URL`.

**Acceptance:** a test asserts that `ots_app` cannot `CREATE TABLE`. A test asserts the hasher parameters.

**Prompt**
```
Implement Step M00.3. Settings split, JSON logging with request_id, an Argon2 hasher subclass with m=19456, t=2, p=1, and database roles per contract section 9 via a raw-SQL migration plus migrate.sh using the migrator URL.
Tests: the hasher parameters; ots_app lacks CREATE privilege (use a separate connection with the ots_app credentials in the test DB setup).
```

## Step M00.4 — Core primitives

**Specification:** in `ots/core/`:

- `ids.py`: `uuid7()`.
- `clock.py`: `now()` and `today_local()`, using `Asia/Kolkata`.
- `errors.py`: `OtsError` base, plus `PermissionDenied(403)`, `NotFound(404)`, `Conflict(409, current=…)`, `InvalidTransition(409)`, `ValidationFailed(422, fields=…)`, `QuotaBlocked(507)`, `StorageUnavailable(503)`, `Locked(423)`.
- `middleware.py`:
  - `RequestIdMiddleware`: accepts an incoming `X-Request-Id` or generates one, and echoes it back.
  - `ErrorMappingMiddleware`: maps `OtsError` to JSON for HTMX/JSON requests, or to an error template for full pages.
  - Never leaks a stack trace in production.
- `permissions.py`: the `require(user, action, obj=None)` registry. Modules register policy functions as `@policy('task.view')`. An unknown action raises (fail closed).

**Acceptance:** unit tests for each error mapping; an unknown policy fails closed; request IDs propagate to log records.

**Prompt**
```
Implement Step M00.4: ots/core ids, clock, errors, middleware and the permission registry, exactly per the spec. Permission checks must fail closed on unknown actions.
Tests are listed in the step.
```

## Step M00.5 — Idempotency layer

**Specification**

- Model `IdempotencyRecord`, columns per contract section 6.
- Middleware `IdempotencyMiddleware`:
  1. Applies to POST, PUT, PATCH and DELETE under `/app/` and `/api/`.
  2. A missing `Idempotency-Key` returns 428, except on endpoints decorated `@idempotency_exempt` (login, SSE).
  3. Key format: a UUID; otherwise 400.
  4. Before the view runs: `SELECT … FOR UPDATE` on the key.
     - If a record exists with the same user, method and path, return the stored status and body, plus header `Idempotent-Replay: true`.
     - If it exists with a different user or path, return 422.
  5. After the view, store the status and the gzip'd body if the status is below 500. Do not store 5xx responses, so the client may retry.
  6. The store happens in the same DB transaction as the view (`ATOMIC_REQUESTS` scoped to these methods), so a stored response always means the change committed.
- Job `sync.purge_idempotency` deletes records older than 7 days.

**Failure:** F-CORE-1 (duplicate request returns the same response, one DB row created).

**Acceptance:** `tests/failure/test_F_CORE_1.py` sends the same POST 3 times concurrently (threads) and gets 1 row and 3 identical responses.

**Prompt**
```
Implement Step M00.5, the idempotency layer, exactly per spec, including the concurrency-safe FOR UPDATE row lock and replay semantics. Use a test-only view that creates a row.
Write tests/failure/test_F_CORE_1.py using 3 concurrent threads with a transactional test DB (pytest-django transaction=True).
```

## Step M00.6 — Event outbox and dispatcher

**Specification**

- Table `outbox_events`, per contract section 6.
- Reusable migration operation `AppendOnly(table, allow_update_columns=())`. It creates a trigger that raises on UPDATE (except on the allowed columns) and on DELETE. It revokes UPDATE and DELETE from `ots_app`. For `outbox_events`, `published_at` is set only through the SECURITY DEFINER function `ots_mark_published(id)`.
- `events.publish(type, aggregate, payload)`:
  - must be called inside `transaction.atomic()`; it raises if `connection.in_atomic_block` is false;
  - inserts a row;
  - registers `transaction.on_commit(lambda: pg_notify('ots_events', str(id)))`.
- Dispatcher (`live.dispatch_outbox` job, in the worker):
  - `LISTEN ots_events`;
  - on a notification, or every 5 s, fetches unpublished rows in id order (batches of 100) and calls registered handlers (`@subscribe('task.assigned')`);
  - marks the row published only after all handlers succeed;
  - on a handler exception: logs, raises alert `outbox.handler_failed`, leaves the row unpublished, and retries with backoff up to 10 times. Then it marks the row as dead-lettered in the payload via the function and raises a CRITICAL alert.
- Handlers must be idempotent: they receive the `event_id`.

**Failures:**

- F-CORE-3: the worker is killed after the business commit but before dispatch. On restart the event is dispatched exactly once (at-least-once delivery plus idempotent handlers).
- The append-only test: UPDATE and DELETE on `outbox_events` as `ots_app` both raise.

**Prompt**
```
Implement Step M00.6: the outbox_events table, the AppendOnly migration operation (trigger + revoke), the ots_mark_published SECURITY DEFINER function, events.publish(), the subscriber registry and the dispatcher loop.
Tests: publish outside atomic raises; on_commit notify; handler failure leaves the event unpublished and retries; tests/failure/test_F_CORE_3.py simulates a crash between commit and dispatch and asserts single delivery via an idempotent handler that counts by event_id; append-only trigger tests.
```

## Step M00.7 — Job scheduler and worker

**Specification**

- Tables `jobs` and `job_runs`, per contract section 6.
- Registry: `@job('attendance.rollup', schedule='45 22 * * *', heavy=False)`. Schedules are evaluated in Asia/Kolkata with `croniter`. `schedule='continuous'` means the job runs on every loop tick.
- `manage.py run_worker`:
  1. Sync the registry to the `jobs` table on start: insert missing jobs; keep `enabled` flags.
  2. Loop every 5 s (or wake on NOTIFY):
     - for each due job, `pg_try_advisory_lock(hashtext(name))`;
     - heavy jobs also take `ots_heavy`;
     - run it in try/except;
     - record a `job_runs` row;
     - set `next_run_at`.
  3. Touch `/tmp/worker-heartbeat` each loop.
  4. On SIGTERM, finish the current job and exit within 30 s.
  5. **Missed runs:** if `next_run_at` is in the past at start-up (for example the server was off at 23:00), run once immediately. Do not catch up multiple missed runs.
- **Watchdog:** job `jobs.watchdog` every minute raises alert `job.overdue.<name>` if a scheduled job's `next_run_at` is more than 30 min in the past, or the last run failed twice in a row.
- Every job must be **safe to re-run**: it must resume from persisted state and never double-apply.

**Failure:** F-CORE-2. Kill the worker in the middle of a job. On restart the job re-runs safely, and the previous `job_runs` row is closed as `ABORTED` by a start-up sweep.

**Prompt**
```
Implement Step M00.7: the job registry, the jobs and job_runs tables, the run_worker command with advisory locks and the heavy mutex, missed-run handling, heartbeat file, graceful SIGTERM, and the watchdog job.
Tests: schedule evaluation in IST across midnight; two worker processes never run the same job concurrently (use two DB connections); the heavy mutex serialises; tests/failure/test_F_CORE_2.py (kill mid-job → ABORTED → re-run succeeds once).
```

## Step M00.8 — Alerts API and health endpoint

**Specification**

- `alerts.raise_alert(key, severity, title, detail)`:
  - upserts by `key`: increments `count`, updates `last_seen_at`, and reopens the alert if it was resolved.
  - publishes event `alert.raised`, so the Super Admin sees it live.
- `alerts.resolve_alert(key)`.
- Every alert key is a stable string listed in `21_FAILURE_TEST_CATALOGUE.md`.
- `/healthz` (no auth; LAN only via Caddy matcher) returns `db`, `disk` and `worker_heartbeat_age_s`. HTTP 503 if any is critical.

**Prompt**
```
Implement Step M00.8: the alerts model and API with dedupe, the alert.raised event, and the /healthz extension. Tests cover dedupe, reopen and the 503 conditions.
```

## Step M00.9 — Deploy, rollback and CA scripts

**Specification**

- `deploy/scripts/install.sh` (first install):
  - creates `/srv/data/*` directories with owners;
  - copies `.env.example` to `/etc/ots/.env` if it is missing;
  - sets file modes;
  - builds the images and runs `migrate.sh`;
  - creates the first Super Admin and the break-glass account (via `manage.py bootstrap_admins`, M02);
  - starts the stack.
- `deploy/scripts/deploy.sh <git-ref>`:
  1. Tag the current images `ots-app:previous`.
  2. Build the new images, tagged `ots-app:<ref>`.
  3. `pg_dump` pre-migration export to `/srv/data/backup-local/pre-migrate-<ts>.dump`, plaintext, local only, keep the last 3.
  4. Run migrations.
  5. `docker compose up -d`.
  6. Poll `/healthz` for 90 s.
  7. If it is unhealthy, run `rollback.sh` automatically and exit 1.
- `deploy/scripts/rollback.sh`:
  - re-tags `previous` as current and runs `docker compose up -d`;
  - **does not** reverse migrations; it prints Runbook E instructions if the migration list changed.
- Caddy serves `/ca.crt` on port 80 (the M00.2 Caddyfile). The P0-D scripts install it on PCs.

**Failure:** F-IN-3. Deploy a build whose `/healthz` returns 503. The script rolls back automatically and the previous version serves traffic.

**Prompt**
```
Implement Step M00.9: install.sh, deploy.sh and rollback.sh exactly per spec. They must be shellcheck clean and use set -euo pipefail.
Add tests/failure/test_F_IN_3.sh (a bats or bash test using a fake broken image tag that makes /healthz return 503) proving automatic rollback. Document manual verification steps in docs/CHANGELOG.md.
```

---

## M00 done-gate

- [ ] All 9 steps merged; lint, typecheck and test are green.
- [ ] Failure tests F-CORE-1, F-CORE-2, F-CORE-3, F-DB-1 (compose restart), F-IN-3 pass.
- [ ] The stack runs on the office server; `https://tasks.office.lan/healthz` is OK from a PC with the root CA installed.
