# M04 — Backup and Disaster Recovery (RPO ≤ 15 min, RTO ≤ 4 h)

**Depends on:** M00, and M05.1 to M05.4 (StorageProvider, GraphProvider and the operations queue).
**Must be live before any business data is entered.**
**Produces:**

- WAL archiving: a local plaintext copy, an encrypted spool, and shipping;
- the recovery-point (RPO) monitor;
- weekly base backup;
- nightly logical export and system set;
- verification jobs;
- the pruner;
- restore tooling;
- the backup section of the health page.

**Failure IDs:** F-BK-1 to F-BK-6, F-DB-2, F-DB-3 (WAL side).

## 1. WAL archive script (`deploy/postgres/ots-wal-archive.sh`)

PostgreSQL runs this inside the `db` container: `archive_command = '/usr/local/bin/ots-wal-archive.sh %p %f'`.

```
Inputs:  $1 = %p (path of the segment, relative to PGDATA), $2 = %f (file name)
Env:     OTS_WAL_LOCAL=/srv/data/wal-local  OTS_WAL_SPOOL=/srv/data/wal-spool  OTS_AGE_PUB=/etc/ots/backup.pub
Steps (set -euo pipefail):
 1. Local plaintext copy: target=$OTS_WAL_LOCAL/$2.zst
    - if target exists: compare `zstd -dc target | sha256sum` with `sha256sum $1`.
      Equal → continue to step 2. Different → exit 1 (never overwrite).
    - else: zstd -q -3 -c $1 > target.tmp ; sync target.tmp ; mv target.tmp target ; sync dir
 2. Encrypted spool copy: target=$OTS_WAL_SPOOL/$2.zst.age
    - if it exists, or a marker $OTS_WAL_SPOOL/.shipped/$2 exists → exit 0 (already handled)
    - else: zstd -q -3 -c $1 | age -R $OTS_AGE_PUB > target.tmp ; sync ; mv ; sync dir
 3. exit 0
No network calls. Must finish in under 2 s for a 16 MB segment on the target hardware.
```

- Disk guard: if the free space on `/srv/data` is below 5%, exit 1. PostgreSQL then retains WAL in `pg_wal` and retries. Alert F-BK-4 has already fired at 75% of the spool cap.

## 2. WAL shipper (worker; `storage_operations` kind `SHIP_WAL`, highest priority)

1. The job `backup.enqueue_wal` (continuous) scans the spool. For each `*.zst.age` without a `storage_operations` row, it inserts `SHIP_WAL` (`target_library=DR`, `remote_path=PostgreSQL/WAL/<timeline>/<segment>.zst.age`, sha256 of the encrypted file).
2. The storage queue (M05) processes `SHIP_WAL` before every other kind, in **segment-name order**.
3. On DONE, in one transaction:
    - insert `backup_objects` (type WAL, segment, `wal_end_lsn`, `wal_end_time`);
    - move the spool file to `.shipped/<segment>` as a zero-byte marker, then delete the encrypted file.
    - `wal_end_time` is the segment's last-modified time, which approximates the archive time. This is acceptable because `archive_timeout = 300`.
4. Markers older than 30 days are pruned.

## 3. Recovery-point monitor (`backup.rpo_monitor`, every 60 s)

- `rpo_age = now − max(wal_end_time) over confirmed WAL backup_objects`.
- Below 10 min: resolve alert `backup.rpo`. 10 to 15 min: WARNING. Over 15 min: CRITICAL.
- The health card shows the value in minutes.
- It also tracks spool size: WARNING at 6 GB, CRITICAL at 7.5 GB (`backup.spool_size`).
- It also tracks `pg_stat_archiver.failed_count` growth: CRITICAL `backup.archiver_failing`.

## 4. Base backup (`backup.base`, Sunday 01:00, heavy)

1. `pg_basebackup -h db -U ots_backup -D - -Ft -X none --checkpoint=fast | zstd -3`, run in the `db` container through `docker exec`, or from the worker over the network with the `ots_backup` role. Prefer the network route: the worker image includes the `postgresql-client-16` package.
2. Write the plaintext copy `/srv/data/backup-local/base-<date>.tar.zst`; keep only the latest.
3. Encrypt with `age -R` to staging, then upload as `UPLOAD_BACKUP_OBJECT` to `DR:PostgreSQL/Base-Backups/YYYY/MM/DD/base.tar.zst.age`.
4. Record the start and stop LSN from the `backup_label` in `backup_runs.detail`.
5. On failure, retry once at 02:00 and raise alert `backup.base_failed`.

## 5. Nightly logical export and system set (`backup.nightly_logical`, 23:00, heavy)

| Step | Action | On failure |
| --- | --- | --- |
| 1 | Preconditions: DB healthy; free disk ≥ 2× the previous set size; Graph token obtainable | `backup_runs.status=FAILED`, `step=precheck`, alert |
| 2 | `pg_dump -Fc -Z0 --no-owner` → zstd → `db.dump.zst`; `pg_dumpall --globals-only` → `globals.sql` | FAILED, `step=dump` |
| 3 | System set: `config.tar` (`/deploy`, Caddy config and CA data, `/etc/ots` **excluding** `graph.key` and `.env` secrets; a redacted `.env` listing keys only); `selfies.tar` (files inside the retention window); `blob_manifest.csv` (sha256, size, file_code, drive_id, item_id, state); `release.json` (git SHA, image digests, migration list) | FAILED, `step=pack` |
| 4 | `SHA256SUMS` and `backup_report.json` (row counts per table, sizes, versions, timings) | — |
| 5 | Keep a plaintext copy of `db.dump.zst` + `globals.sql` in `/srv/data/backup-local/` (latest only) for automated restore tests | — |
| 6 | Encrypt each artefact with `age -R` into `/srv/data/backup-staging/bkp-YYYYMMDD-2300/` | FAILED, `step=encrypt` |
| 7 | Enqueue `UPLOAD_BACKUP_OBJECT` operations to `DR:Daily-Logical/YYYY/MM/bkp-YYYYMMDD-2300/`; `backup_report.json` goes last | — |
| 8 | Once all ops are DONE, **verify**: download each object and compare SHA-256 with the encrypted local file | PARTIAL, `step=verify`, alert |
| 9 | Status SUCCESS; delete staging for this set; keep the 3 most recent unconfirmed sets | — |

- Retries at 00:00, 01:30 and 03:30, as separately scheduled jobs that act only if tonight's run is not SUCCESS.
- If tonight is not SUCCESS by 03:45: CRITICAL alert `backup.nightly_failed`, with the failed step in the title.
- **Watchdog (F-BK-1):** at 23:30, if no `backup_runs` row of type LOGICAL started today, raise CRITICAL `backup.nightly_not_started`.

## 6. Verification jobs

| Job | Action | Pass |
| --- | --- | --- |
| `backup.wal_continuity` (04:00) | List confirmed WAL `backup_objects` since the oldest retained base backup's start LSN; check every segment name is consecutive per timeline | No gaps. On a gap: CRITICAL `backup.wal_gap` and **trigger an immediate base backup** (F-BK-5) |
| `backup.test_logical_restore` (Sun 03:00, heavy) | Start a throwaway `postgres:16` container (`--memory=400m`, `tmpfs` data dir); `pg_restore` `backup-local/db.dump.zst`; compare row counts with `backup_report.json`; remove the container | Counts equal. Write a `recovery_tests` row |
| `backup.test_pitr` (1st Sun 03:30, heavy) | Throwaway container; restore `backup-local/base-*.tar.zst`; `restore_command` reads from `wal-local`; `recovery_target_time` = the time of a **canary row** inserted at a random minute in the last 7 days by the job `backup.canary` (hourly, office hours; inserts into the table `dr_canaries`) | The canary at the target is present; the next canary (≥ 1 h later) is absent |
| `backup.key_test` (quarterly, **manual-assisted**) | The Super Admin runs `manage.py dr_keytest` on the offline laptop with the sealed key. It downloads the latest set and WAL sample and decrypts them | The command prints PASS; the result is recorded by uploading the signed result file, or entered in the UI |

## 7. Retention and pruning (`backup.prune`, 03:00)

1. Compute the **keep set** first:
    - the 8 newest weekly base backups, plus the first base backup of each of the last 12 months;
    - every WAL segment with LSN ≥ the start LSN of the oldest weekly base backup kept;
    - logical sets: 30 daily, 12 monthly (1st of month), 8 yearly (1 January).
2. Delete only DR objects **outside** the keep set, and never fewer than the minimums above. Uses `StorageProvider.delete_dr(remote_ref)`; this is the only delete method, and it rejects any path outside the DR drive.
3. Each delete sets `backup_objects.pruned_at`. An interrupted prune restarts from the keep-set computation.
4. Local: `wal-local` older than 7 days; `.shipped` markers older than 30 days; `backup-staging` sets already SUCCESS.

## 8. Restore tooling (`manage.py dr_*`; used by the runbooks)

| Command | Purpose |
| --- | --- |
| `dr_list` | List restore points: base backups, the WAL range, logical sets |
| `dr_fetch --base <id> --until <ts> --dest /srv/restore` | Download the base backup and the needed WAL segments from SharePoint (still encrypted) |
| `dr_decrypt --identity <path-to-private-key> --src /srv/restore` | Run on the restore machine only, with the private key from the sealed envelope |
| `dr_restore_pitr --data /srv/restore/pgdata --target "<ts>"` | Unpack the base backup, write `recovery.signal`, set `restore_command = 'zstd -dc /srv/restore/wal/%f.zst > %p'` and `recovery_target_time`, start PostgreSQL, wait for recovery, run integrity checks |
| `dr_restore_logical --set <id>` | `pg_restore` into a new database |
| `dr_integrity_check` | FK validation queries, row counts compared with the last report, append-only trigger presence, sample of 20 file versions resolvable in Graph |

## Steps and prompts

### M04.1 — WAL archive script + tests
```
Read CLAUDE.md, docs/plan/01_ARCHITECTURE_CONTRACT.md and docs/plan/M04_BACKUP_DR.md. Implement Step M04.1: deploy/postgres/ots-wal-archive.sh exactly per section 1. Rebuild the db image.
Tests (tests/backup/test_wal_archive.sh with bats, plus a pytest wrapper):
- a new segment creates both copies atomically (no .tmp left);
- an identical re-run exits 0;
- a different-content collision exits 1 and leaves the original intact;
- a low-disk simulation (small tmpfs) exits 1;
- timing under 2 s for 16 MB.
Integration test: generate WAL with pg_switch_wal() 3 times and assert 3 files in wal-local and 3 in wal-spool.
```

### M04.2 — Shipper + RPO monitor
```
Implement Step M04.2: backup.enqueue_wal, SHIP_WAL handling inside the M05 storage queue (priority 0, ordered by segment name), backup_objects recording, the .shipped markers, and the backup.rpo_monitor job with the thresholds and alerts in section 3.
Tests with the respx-mocked GraphProvider:
- tests/failure/test_F_BK_3.py: Graph unreachable for 20 simulated minutes → WARNING at 10, CRITICAL at 15; after recovery, segments ship in order and the alert resolves.
- tests/failure/test_F_BK_4.py: spool over 6 GB (sparse files) → WARNING.
```

### M04.3 — Base backup
```
Implement Step M04.3: the backup.base job per section 4 (pg_basebackup over the network as ots_backup, zstd, local plaintext latest, age-encrypted upload, start/stop LSN recorded, retry at 02:00). Add the ots_backup role grants (REPLICATION) and pg_hba entry.
Test: run against the dev db; assert backup_runs SUCCESS, an object enqueued, and backup_label parsed.
```

### M04.4 — Nightly logical + system set + verify + retries
```
Implement Step M04.4: backup.nightly_logical, the steps table in section 5 exactly, including the redaction of secrets, the plaintext local copy, encryption, upload ops, download-verify, the PARTIAL/FAILED/SUCCESS semantics, retry jobs at 00:00/01:30/03:30, the 03:45 critical alert and the 23:30 not-started watchdog.
Tests:
- tests/failure/test_F_BK_1.py (watchdog);
- test_F_BK_2.py (verify mismatch → PARTIAL and alert; previous SUCCESS kept);
- a secrets redaction test (no value of any .env key appears in config.tar).
```

### M04.5 — Verification jobs + canary
```
Implement Step M04.5: dr_canaries table + backup.canary job; backup.wal_continuity (gap → CRITICAL + immediate base backup); backup.test_logical_restore and backup.test_pitr using throwaway containers via the Docker socket.
Mount /var/run/docker.sock into the worker read-write ONLY for this purpose. Restrict calls to a wrapper module ots/backup/sandbox.py that only allows `docker run` of the postgres:16 image with --memory=400m and a tmpfs volume, and `docker rm`. Document this in decisions.md.
Write recovery_tests rows.
Tests: tests/failure/test_F_BK_5.py (delete one WAL backup_object → gap detected, base backup enqueued); test_F_BK_6.py (corrupt backup-local dump → test fails, CRITICAL alert, earlier good points untouched).
```

### M04.6 — Pruner
```
Implement Step M04.6: backup.prune per section 7. Compute the keep set first; delete only outside it, never below the minimums; DR drive only (provider.delete_dr rejects any other drive ID). Also local pruning.
Tests: a synthetic 400-day history → exact expected keep set; an interrupted prune (exception after 3 deletes) followed by a re-run gives the same final state; delete_dr on the Business drive raises.
```

### M04.7 — Restore tooling
```
Implement Step M04.7: the management commands dr_list, dr_fetch, dr_decrypt, dr_restore_pitr, dr_restore_logical and dr_integrity_check per section 8. dr_decrypt must refuse to run if /etc/ots exists on the machine (a guard against putting the private key on the production server) unless --i-am-not-production is passed.
Test: a full round trip in CI with a generated test age key: insert data → ship (mock Graph backed by a local dir) → fetch → decrypt → PITR to timestamp → integrity check passes.
```

### M04.8 — Health card
```
Implement Step M04.8: a backup card on the System health page (M12 builds the page; create the card partial now):
- recovery-point age in minutes (colour by threshold);
- last WAL shipped;
- spool size;
- last nightly status and step;
- last base backup;
- last logical and PITR test results;
- next scheduled runs.
```

## M04 done-gate (P1 Protection gate)

- [ ] On the real server with the real tenant: WAL is shipping and the RPO age stays under 10 min for 24 h.
- [ ] A nightly SUCCESS with download-verify.
- [ ] Logical restore and PITR tests pass.
- [ ] The key test has been done once with the sealed key.
- [ ] F-BK-1 to F-BK-6 are green.
