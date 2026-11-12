# 01 — Architecture Contract (frozen)

The rules here are fixed. Changing any of them requires an entry in `docs/decisions.md`, approved by the Super Admin.

## 1. Frozen principles

1. PostgreSQL is the only authoritative store of business records.
2. OneDrive for Business (a SharePoint site) is an external dependency behind `ots.storage.StorageProvider`. No other package knows it exists.
3. Business files go to library `Business-Files`. Recovery data goes to library `System-DR`. Recovery data never appears in the app UI.
4. Nothing is reported as saved, archived or backed up until it is verified.
5. Every write carries an idempotency key. Retries never create duplicates.
6. A business change and its audit or event row commit in one transaction.
7. History is append-only. Corrections are new records.
8. Identity is account-based. The server clock is authoritative.
9. Sessions, idle logout and permissions are enforced by the server.
10. Business actions are sent immediately. Nothing is batched on a timer.
11. Every module implements its failure IDs (see `21_FAILURE_TEST_CATALOGUE.md`).
12. Off-site recovery point (RPO) is 15 minutes or less, and is monitored. Degradation is always shown.
13. Backups are proven by scheduled restores.
14. Scaling happens by configuration or by adding components, never by rewrite.
15. Every deviation from the plan is logged in `docs/decisions.md`.

## 2. Data classes

| Class | Examples | Owner | Off-site protection |
| --- | --- | --- | --- |
| Records | users, tasks, events, sessions, attendance, file metadata | PostgreSQL | WAL every 5 min + weekly base backup + 23:00 logical export → `System-DR` |
| Documents | uploaded file bytes | `/srv/data/blobs` until `ARCHIVED`, then `Business-Files` | The archive itself |
| Selfies | login photos | `/srv/data/selfies` | Inside the encrypted nightly system set only. **Never** sent to `Business-Files` |
| Pending client actions | unacknowledged actions | PWA IndexedDB outbox | None. Kept short-lived by sending immediately |
| Config and keys | `.env`, Caddy CA, Graph cert | `/etc/ots`, Caddy volume | Nightly system set (secrets excluded), plus a sealed offline copy |

## 3. Runtime topology

| Container | Image | Memory limit | Role |
| --- | --- | --- | --- |
| `caddy` | `caddy:2` | 64 MB | TLS (`tls internal`), reverse proxy, 210 MB request-body limit, zstd/gzip compression |
| `app` | built from `/app` | 600 MB | Uvicorn, 1 worker, ASGI Django: pages, HTMX, SSE, uploads |
| `worker` | same image | 350 MB | `manage.py run_worker`: job scheduler, storage queue, WAL shipper, backups, sweeps |
| `db` | built from `/deploy/postgres` (postgres:16 + age + zstd) | 1 GB | PostgreSQL with WAL archiving |

**Volumes (bind mounts):**

- `/srv/data/pg`: PostgreSQL data directory.
- `/srv/data/wal-spool`: encrypted WAL waiting to ship.
- `/srv/data/wal-local`: plaintext zstd WAL, kept 7 days.
- `/srv/data/blobs`: file contents.
- `/srv/data/staging`: in-flight uploads.
- `/srv/data/selfies`.
- `/srv/data/backup-staging`.
- `/srv/data/backup-local`: latest base backup and logical export, plaintext.
- `/etc/ots`: mounted read-only into `app`, `worker` and `db`.

**Ports:** only `caddy` publishes 80 and 443. `db` is reachable only on the internal Docker network.

## 4. Budgets

### RAM (steady state, about 1.05 GB)

| Component | Allocation |
| --- | --- |
| OS + Docker | ~450 MB |
| PostgreSQL | ~250 MB |
| app | ~180 MB |
| worker | ~120 MB |
| caddy | ~30 MB |

PostgreSQL settings:

- `shared_buffers=128MB`
- `work_mem=4MB`
- `maintenance_work_mem=64MB`
- `max_connections=20`
- `effective_cache_size=1GB`
- `wal_compression=zstd`
- `archive_mode=on`
- `archive_timeout=300`
- `max_wal_size=1GB`
- `min_wal_size=80MB`

### Disk (80 GB)

| Use | Cap | Guard |
| --- | --- | --- |
| OS, images, logs | 18 GB | Log rotation at 500 MB per container |
| PostgreSQL data + `pg_wal` | 6 GB | Alert at 4 GB |
| `wal-spool` | 8 GB | Alert at 6 GB |
| `wal-local` | 2 GB | Kept 7 days, then pruned |
| `backup-staging` + `backup-local` | 5 GB | Keep 3 unconfirmed sets + latest local plaintext |
| `staging` + `blobs` (cache) | 25 GB | Evict ARCHIVED blobs LRU above 25 GB; refuse uploads at 90% total disk |
| `selfies` | 1 GB | 180-day retention |
| Reserve | ~15 GB | Never used by design |

### Network

- Uploads go from browser to server in 8 MiB pieces.
- Server to Graph:
  - single PUT for files of 4 MB or less;
  - otherwise a resumable upload session with 5 MiB pieces (16 × 320 KiB).
- Shipper bandwidth: 50% of uplink from 09:00 to 19:00 IST, unrestricted otherwise. Setting: `OTS_UPLINK_MBPS`, default 50.

## 5. Master enum list (persist these exact values)

```
Role:                SUPER_ADMIN | SUB_ADMIN | EMPLOYEE | DISPLAY(service, not a person)
Qualification:       TENTH | TWELFTH | DIPLOMA | GRADUATE | POSTGRADUATE | DOCTORATE | PROFESSIONAL
Priority:            LOW | NORMAL | HIGH | URGENT
AssignmentStatus:    ASSIGNED | IN_PROGRESS | PAUSED | BLOCKED | SUBMITTED | CHANGES_REQUESTED | APPROVED | REMOVED | CANCELLED
PauseReason:         END_OF_DAY
BlockReason:         INSUFFICIENT_INPUT | DEPENDENCY
TaskDerivedStatus:   ASSIGNED | IN_PROGRESS | AWAITING_REVIEW | COMPLETED | CANCELLED
FileRelation:        REFERENCE | SUBMISSION | DELIVERABLE
FileVisibility:      PRIVATE | TASK | LIBRARY
FileState:           INITIATED | RECEIVING | REJECTED | ABANDONED | STAGED | ARCHIVING | RETRY_WAIT |
                     BLOCKED_QUOTA | BLOCKED_AUTH | VERIFYING | ARCHIVED | FAILED | MISSING | DRIFTED
StorageOpKind:       ARCHIVE_FILE | SHIP_WAL | UPLOAD_BACKUP_OBJECT | REPAIR_FILE
StorageOpState:      PENDING | RUNNING | RETRY_WAIT | BLOCKED_QUOTA | BLOCKED_AUTH | DONE | FAILED
SessionEndReason:    END_OF_DAY | MANUAL | IDLE_45 | BROWSER_CLOSED | FORCED
IdleMode:            OS | TAB_ONLY
AttendanceStatus:    P (present) | L (late) | A (absent) | WO (weekly off) | H (holiday)
BackupType:          WAL | BASE | LOGICAL | SYSTEM_SET
BackupRunStatus:     RUNNING | SUCCESS | FAILED | PARTIAL
RecoveryTestType:    LOGICAL_RESTORE | PITR | KEY_TEST | DR_EXERCISE
AlertSeverity:       INFO | WARNING | CRITICAL
SyncEventState:      PENDING | SENDING | ACKNOWLEDGED | CONFLICT | REJECTED   (client side)
```

## 6. Schema master list

All primary keys are UUIDv7 unless noted. All timestamps are `timestamptz`. `company_id` (UUID, FK `companies`) is on `users`, `tasks` and `files` from day one, with a single default company.

| Table | Module | Key columns |
| --- | --- | --- |
| `companies` | M02 | id, name, code |
| `departments` | M02 | id, company_id, name, is_active |
| `users` | M02 | id, company_id, employee_id (unique), login_id (unique, case-insensitive), full_name, password, role, reports_to (FK self, PROTECT, nullable only for SUPER_ADMIN), designation, department_id, date_of_joining, highest_qualification, qualification_detail, institution, qualification_year, shift_start, shift_end, weekly_off (int[] 0=Mon…6=Sun), mobile, email, reference_photo_path, is_active, is_break_glass, must_change_password, failed_logins, locked_until, last_login_at, created_at, version |
| `reporting_history` | M02 | id, user_id, manager_id, effective_from, effective_to (null = current), changed_by, created_at |
| `audit_log` | M03 | id (bigserial), at, actor_id, action, target_type, target_id, payload jsonb, client_ip inet, request_id. **Append-only** |
| `outbox_events` | M00 | id (bigserial), at, type, aggregate_type, aggregate_id, payload jsonb, published_at. **Append-only** apart from `published_at`, which is set by the dispatcher via a SECURITY DEFINER function |
| `idempotency_records` | M00 | key (pk), user_id, method, path, status_code, content_type, response_body bytea (gzip), created_at. Purged after 7 days |
| `jobs` | M00 | name (pk), schedule (cron string, Asia/Kolkata), enabled, last_started_at, last_finished_at, last_status, last_error, next_run_at |
| `job_runs` | M00 | id, job_name, started_at, finished_at, status, detail jsonb |
| `alerts` | M00/M12 | id, key (dedupe), severity, title, detail, first_seen_at, last_seen_at, count, resolved_at |
| `work_sessions` | M06 | id, user_id, work_date, login_at, selfie_path, idle_mode, idle_started_at, active_resumed_at, live_connected, live_disconnected_at, logout_at, logout_reason, active_seconds, client_ip, user_agent, device_id |
| `attendance_days` | M06 | (user_id, date) pk, status, first_login, last_logout, active_minutes, sessions, idle_logouts, late_minutes, computed_at |
| `attendance_corrections` | M06 | id, user_id, date, new_status, reason, requested_by, approved_by, approved_at, created_at |
| `holidays` | M06 | date pk, name, company_id |
| `tasks` | M07 | id, company_id, task_code (unique), title, description, priority, category, created_by, created_at, scheduled_start_at, due_at, derived_status, has_blocked, is_cancelled, hide_from_display, completed_at, version, search_vector (generated tsvector) |
| `task_assignments` | M07 | id, task_id, user_id, assigned_by, assigned_at, status, pause_reason, block_reason, block_note, blocked_since, blocked_seconds, started_at, submitted_at, submitted_late, approved_at, removed_at, version. Partial unique on (task_id, user_id) where removed_at is null |
| `task_comments` | M07 | id, task_id, assignment_id, author_id, body, created_at. Immutable |
| `task_events` | M07 | id (bigserial), task_id, assignment_id, actor_id, event_type, payload, created_at. **Append-only** |
| `files` | M08 | id, company_id, display_name, owner_id, visibility, library_folder_id, current_version_id, created_at |
| `file_versions` | M08 | id, file_id, version_number, file_code (unique), original_filename, blob_sha256, mime_type, size_bytes, uploaded_by, uploaded_at, note, state (FileState), idempotency_key (unique) |
| `blobs` | M08 | sha256 (pk), size_bytes, local_path, cached bool, last_accessed_at, drive_id, item_id, etag, ms_quick_xor_hash, archived_at, state (FileState) |
| `upload_sessions` | M08 | id, idempotency_key (unique), user_id, task_id, file_id (null = new file), expected_size, expected_sha256, original_filename, received_bytes, created_at, last_piece_at, state |
| `task_files` | M08 | id, task_id, file_id, version_id, assignment_id, relation, linked_by, linked_at |
| `library_folders` | M08 | id, parent_id, name |
| `file_handovers` | M08 | id, version_id, from_user, task_id, note, sent_at |
| `file_handover_recipients` | M08 | id, handover_id, to_user, first_opened_at |
| `file_access_log` | M08 | id (bigserial), version_id, user_id, action (DOWNLOAD/PREVIEW), source (CACHE/ONEDRIVE), at. **Append-only** |
| `storage_operations` | M05 | id, kind, state, target_library (BUSINESS/DR), local_path, remote_path, sha256, size_bytes, ref_type, ref_id, attempts, next_attempt_at, upload_url (encrypted), confirmed_bytes, last_error, drive_id, item_id, created_at, updated_at |
| `storage_cursors` | M05 | library pk, delta_link, updated_at |
| `storage_capacity` | M05 | id, at, used_bytes, total_bytes |
| `backup_runs` | M04 | id, type, started_at, finished_at, status, step, detail jsonb, size_bytes |
| `backup_objects` | M04 | id, run_id, type, name, remote_path, sha256, size_bytes, wal_segment, wal_end_lsn, wal_end_time, confirmed_at, pruned_at |
| `recovery_tests` | M04 | id, type, started_at, finished_at, passed, detail jsonb |
| `notifications` | M10 | id, user_id, kind, task_id, payload, created_at, read_at |
| `display_devices` | M10 | id, name, token_hash, created_by, created_at, last_seen_at, revoked_at |
| `display_pairing_codes` | M10 | code pk, created_by, expires_at, used_at |

## 7. Job schedule (all times Asia/Kolkata)

| Job | Schedule | Module |
| --- | --- | --- |
| `storage.process_queue` | continuous (5 s loop) | M05 |
| `backup.ship_wal` | continuous (part of storage queue, highest priority) | M04/M05 |
| `attendance.idle_sweep` | every 60 s | M06 |
| `live.dispatch_outbox` | continuous (LISTEN/NOTIFY + 5 s fallback) | M00/M10 |
| `health.capacity_check` | hourly at :07 | M05/M12 |
| `health.disk_check` | every 5 min | M12 |
| `backup.rpo_monitor` | every 60 s | M04 |
| `attendance.rollup` | 22:45 daily | M06 |
| `backup.nightly_logical` | 23:00 daily (retries 00:00, 01:30, 03:30) | M04 |
| `backup.base` | Sunday 01:00 | M04 |
| `storage.delta_reconcile` | 02:00 daily | M05 |
| `storage.orphan_sweep` | 02:30 daily | M05 |
| `files.cache_evict` | 02:45 daily, and on demand at 90% of cap | M08 |
| `backup.prune` | 03:00 daily | M04 |
| `backup.test_logical_restore` | Sunday 03:00 | M04 |
| `backup.test_pitr` | 1st Sunday monthly 03:30 | M04 |
| `backup.wal_continuity` | 04:00 daily | M04 |
| `storage.full_inventory` | 1st of month 03:00 | M05 |
| `sync.purge_idempotency` | 04:30 daily | M00 |
| `files.staging_sweep` | 04:45 daily | M08 |
| `attendance.selfie_retention` | 05:00 daily | M13 |
| `live.tv_reload` | 03:00 daily (pushes a reload message to displays) | M10 |
| `notifications.deadline_reminders` | every 15 min, 08:00 to 20:00 | M10 |

**Heavy-job mutex:** `backup.nightly_logical`, `backup.base`, `backup.test_*` and `storage.full_inventory` all take the PostgreSQL advisory lock `ots_heavy`. Only one runs at a time.

## 8. Settings keys (`/etc/ots/.env`)

```
DJANGO_SECRET_KEY=
DJANGO_ALLOWED_HOSTS=tasks.office.lan,192.168.x.x
DATABASE_URL=postgres://ots_app:...@db:5432/ots
DATABASE_MIGRATOR_URL=postgres://ots_migrator:...@db:5432/ots
OTS_DATA_DIR=/srv/data
OTS_TIME_ZONE=Asia/Kolkata
OTS_UPLOAD_MAX_MB=200
OTS_CACHE_CAP_GB=25
OTS_UPLINK_MBPS=50
OTS_IDLE_MINUTES=45
OTS_LATE_GRACE_MINUTES=15
OTS_SELFIE_RETENTION_DAYS=180
GRAPH_TENANT_ID=
GRAPH_CLIENT_ID=
GRAPH_CERT_PATH=/etc/ots/graph.pem
GRAPH_KEY_PATH=/etc/ots/graph.key
GRAPH_SITE_ID=
GRAPH_DRIVE_BUSINESS=
GRAPH_DRIVE_DR=
OTS_BACKUP_AGE_PUBLIC_KEY_PATH=/etc/ots/backup.pub
OTS_FIELD_ENCRYPTION_KEY=          # AES-256-GCM key for secrets stored in DB (upload URLs)
```

## 9. Database roles

| Role | Rights |
| --- | --- |
| `ots_migrator` | Owns the schema. Used only by `manage.py migrate` in `deploy.sh` |
| `ots_app` | SELECT/INSERT/UPDATE/DELETE on normal tables; **SELECT/INSERT only** on `audit_log`, `task_events`, `file_access_log`, `outbox_events`; EXECUTE on `ots_mark_published(bigint)` |
| `ots_backup` | `pg_read_all_data` + REPLICATION. Used by `pg_dump` and `pg_basebackup` |
