# 22 — Recovery Runbooks and Operating Procedures

These runbooks are uploaded to `System-DR/Recovery-Documentation/` on every deploy. Keep a printed copy with the sealed key envelope.

**Roles:** the **Operator** is the office admin with server shell access. The **Key holder** is the Super Admin, holding the sealed age key and the break-glass credentials.

---

## Runbook A — Bad change or accidental deletion (target: 1 hour)

**When:** data was wrongly changed or deleted, and you know approximately when.

1. Operator: `docker compose stop app worker`. This stops writes. The TV and PCs show "Server unavailable".
2. Note the incident time T (IST). Pick the target time = T − 1 minute.
3. Choose the source:
    - **If T is within 7 days** (local plaintext WAL is available):
      `manage.py dr_restore_pitr --local --data /srv/restore/pgdata --target "<T-1min IST>"`
    - **Otherwise:** follow Runbook B steps 4 to 7 on this server, using `--target`.
4. `manage.py dr_integrity_check --data /srv/restore/pgdata` must print PASS.
5. Verify with the Super Admin: open the restored database read-only (`dr_preview` starts a temporary app container on port 8443) and confirm the lost data is present.
6. Swap:
    - `docker compose stop db`;
    - move `/srv/data/pg` to `/srv/data/pg-damaged-<date>` (keep it 30 days);
    - move `/srv/restore/pgdata` to `/srv/data/pg`;
    - `docker compose up -d`.
7. Immediately run `manage.py backup_now --base`, so a new timeline gets a fresh base backup.
8. Record the recovery event: `manage.py dr_log --runbook A --note "..."`.

**Lost by design:** any changes made after T−1 min. The employees concerned re-enter them; the pending outbox on their PCs may replay some of them automatically.

## Runbook B — Server destroyed (target: 4 hours)

**Needs:** replacement hardware (or any Linux PC with ≥ 4 GB RAM), internet access, the sealed key envelope, and the break-glass credentials.

1. Install Ubuntu Server 24.04 + Docker (`deploy/scripts/install.sh --restore-mode`, which skips creating new admins).
2. Restore `/etc/ots`:
    - re-create `.env` from the **sealed config sheet** (secret values are not in the backups);
    - regenerate the Graph certificate (P0-A) and upload the new public certificate to the Entra app (Key holder, 10 min).
3. Fetch:
    - `manage.py dr_list` (shows the restore points);
    - `manage.py dr_fetch --base latest --until latest --dest /srv/restore`.
4. Decrypt on this machine **temporarily**:
    - the Key holder scans the QR code into `/root/restore.key` (mode 600);
    - `manage.py dr_decrypt --identity /root/restore.key --src /srv/restore --i-am-not-production`;
    - then **`shred -u /root/restore.key`**.
5. `manage.py dr_restore_pitr --data /srv/data/pg --target latest`.
6. Restore the system set: `manage.py dr_restore_system --set latest`. This covers the Caddy CA (so PCs keep trusting it), selfies and config.
7. `docker compose up -d`, then `manage.py dr_integrity_check` must print PASS.
8. Verify that 20 random files download (they are fetched from Business-Files into the new cache).
9. Super Admin smoke test (login, a task, a file, the TV). Then announce to staff.
10. Update the DHCP reservation if the MAC address changed.

**Data loss:** everything after the last shipped WAL segment. The health page history (if visible) or `dr_list` shows that point.

## Runbook C — OneDrive / SharePoint unavailable or access revoked

| Symptom | Action |
| --- | --- |
| `storage.offline` (internet down) | Nothing is required. Work continues. Check the router or ISP. RPO exposure is shown on the health page |
| `storage.auth` (401/403) | Key holder checks in the Entra admin centre: is the app consent present? Is the certificate expired? Is the site grant present (re-run P0-B)? Then click "Retry now" on the health page |
| `storage.quota` | Buy additional storage, or add a licence (+10 GB per licence to the pool). Remove nothing manually. Click "Retry now" |
| Microsoft-wide outage | Wait. Queues drain automatically |

After any of these: run `manage.py storage_reconcile --now` and confirm queue length 0 and no FAILED operations.

## Runbook D — File missing or changed in SharePoint

1. The health page lists MISSING/DRIFTED versions.
2. If the cache holds the blob, repair is automatic. Confirm it reaches ARCHIVED.
3. If it is not cached: restore the item from the SharePoint site **Recycle bin** (site owner: Super Admin), then run `manage.py storage_reconcile --now`.
4. If it has been purged from the recycle bin: no recovery exists for that version. Mark it as `LOST` with `manage.py file_mark_lost <version_id> --reason`. The record and the trail remain.

## Runbook E — Bad deployment or migration

1. `deploy.sh` rolls back automatically if `/healthz` fails. If problems appear later: run `deploy/scripts/rollback.sh`.
2. If the failed release ran a migration and the old code cannot run on the new schema:
    - stop the app and worker;
    - `pg_restore` the pre-migration dump from `/srv/data/backup-local/pre-migrate-<ts>.dump` into a new database;
    - swap as in Runbook A step 6;
    - run `rollback.sh`.
3. Log the event and the cause in `docs/decisions.md`.

---

## Operating procedures

**OPS-1 Daily (Operator, 2 min):** open the System health page. Everything should be green. Any red alert → follow the runbook linked on the alert.

**OPS-2 New PC onboarding:**

1. Install Chrome or Edge.
2. Run `install-root-ca.ps1` (or `.sh` on macOS).
3. Open `https://tasks.office.lan` and install the app.
4. Run `pwa-autostart.ps1`.
5. Test the camera.
6. First login: enable attendance tracking (the idle permission).

**OPS-3 Fire TV setup:** as in M10 section 3. Pair from Settings → Displays.

**OPS-4 New employee:**

1. The Super Admin creates the manager first, if they are new.
2. Then the employee: profile, reference photo, shift and weekly off.
3. Hand over the temporary password. The employee must change it at first login.

**OPS-5 Graph certificate rotation (yearly, or when `storage.cert_expiring` fires):**

1. Generate a new certificate (P0-A with `--force` to new file names).
2. Upload the public certificate to Entra **alongside** the old one.
3. Update `GRAPH_CERT_PATH` and `GRAPH_KEY_PATH`; restart the worker.
4. Confirm the queue works.
5. Remove the old certificate from Entra.

**OPS-6 Quarterly key test (Key holder, 20 min):** on the offline laptop, run `manage.py dr_keytest` (from a checkout of the repo) against the latest set. Record the result on the health page (Backup card → "Record key test").

**OPS-7 Quarterly DR exercise:** run Runbook B on a spare PC. Record the time against the 4 h target. File the report in `docs/qualification/`.

**OPS-8 Deactivate a leaver:**

1. Reassign their open assignments.
2. Move their reports to someone else, if they are a manager.
3. Deactivate. History is retained.
