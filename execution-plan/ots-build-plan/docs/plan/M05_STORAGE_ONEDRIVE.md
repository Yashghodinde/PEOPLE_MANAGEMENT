# M05 — Storage Integration Service (OneDrive for Business / SharePoint via Microsoft Graph)

**Depends on:** M00, P0.7 to P0.12 (tenant, site, libraries, app with certificate, `Sites.Selected` grant, gate passed).
**Produces:** `ots.storage`. It is the only package that talks to Microsoft.
**Failure IDs:**

- F-ST-1: internet down.
- F-ST-2: throttled.
- F-ST-3: authorisation revoked or certificate expiring.
- F-ST-4: quota.
- F-ST-5: dead letter.
- F-FL-4: orphan remote file.
- F-FL-5: missing remote file.
- F-ST-6: session expired mid-upload.
- F-ST-7: conflict / adopt.

## 1. Interface (`ots/storage/provider.py`)

```python
class Library(StrEnum): BUSINESS = "BUSINESS"; DR = "DR"

@dataclass(frozen=True)
class RemoteRef: library: Library; drive_id: str; item_id: str; etag: str | None

@dataclass(frozen=True)
class RemoteMeta: ref: RemoteRef; name: str; path: str; size: int; quick_xor_hash: str | None; deleted: bool; last_modified: datetime

class StorageProvider(Protocol):
    def put(self, library: Library, remote_path: str, local_path: Path, size: int, sha256: str,
            op: "StorageOperation") -> RemoteRef: ...           # resumable, idempotent on op.id, conflict=fail
    def open_read(self, ref: RemoteRef) -> Iterator[bytes]: ...  # streams 1 MiB chunks
    def head(self, ref: RemoteRef) -> RemoteMeta: ...
    def find_by_path(self, library: Library, remote_path: str) -> RemoteMeta | None: ...
    def changes(self, library: Library, cursor: str | None) -> tuple[list[RemoteMeta], str]: ...  # Graph delta
    def usage(self) -> tuple[int, int]: ...                       # used, total (site/drive quota)
    def delete_dr(self, ref: RemoteRef) -> None: ...              # raises unless ref.library == DR
```

There is no generic delete. Business files are never deleted by code.

## 2. GraphProvider (`ots/storage/graph.py`)

- **Auth:** `msal.ConfidentialClientApplication(client_id, authority=f"https://login.microsoftonline.com/{tenant}", client_credential={"private_key": key, "thumbprint": thumb})`. Scope `https://graph.microsoft.com/.default`. The token is cached in process memory until 5 min before expiry. A certificate expiring within 30 days raises WARNING `storage.cert_expiring`; within 7 days, CRITICAL.
- **HTTP:** a single `httpx.Client` with `timeout=(10 s connect, 120 s read)` and `http2=False`. User-Agent `OTS/<version>`.
- **Path addressing:** `/drives/{drive_id}/root:/{remote_path}:` for create. Once an item ID is known, use `/drives/{drive_id}/items/{item_id}`.
- **Small upload (≤ 4 MiB):** `PUT …:/content?@microsoft.graph.conflictBehavior=fail`.
- **Large upload:**
    1. `POST …:/createUploadSession` with body `{"item": {"@microsoft.graph.conflictBehavior": "fail", "name": <name>}}`.
    2. Save `uploadUrl` (encrypted with `OTS_FIELD_ENCRYPTION_KEY`) and `expirationDateTime` on the operation row.
    3. PUT 5 MiB pieces with `Content-Range`.
    4. After each 2xx/202, persist `confirmed_bytes`.
    5. On resume: `GET uploadUrl` returns `nextExpectedRanges`; continue from its start.
    6. **Never send auth headers to `uploadUrl`.** It is pre-authenticated, as Microsoft specifies.
- **Bandwidth limiter:** a token bucket at `OTS_UPLINK_MBPS × 0.5` between 09:00 and 19:00 IST, unlimited otherwise.
- **Verification after upload:**
    - The final response's `size` must equal the local size. If Graph returns `file.hashes.quickXorHash`, compare it with a locally computed QuickXorHash (implement in `ots/storage/quickxor.py`, unit-tested against Microsoft's published algorithm description).
    - If no hash is returned, `head()` again with retries for up to 60 s.
    - A mismatch raises `VerificationFailed`.
- **Download:** `GET /drives/{d}/items/{i}/content` follows the 302 to a pre-authenticated URL and streams 1 MiB chunks.

## 3. Error policy (implemented in one function, `classify(response | exception) -> Action`)

| Response | Action |
| --- | --- |
| 401 | Refresh the token once and retry once. If it is still 401: BLOCKED_AUTH (the whole queue) + CRITICAL `storage.auth` |
| 403 | BLOCKED_AUTH (whole queue) + CRITICAL `storage.auth`. No automatic retry |
| 404 on `uploadUrl` | Session expired (F-ST-6): `find_by_path`. If the item exists and its size and hash match, adopt it. Otherwise clear `uploadUrl` and start a new session from byte 0 |
| 409 `nameAlreadyExists` | F-ST-7: `find_by_path` and compare size + hash. Match: adopt (DONE). Mismatch: FAILED (dead letter) + CRITICAL `storage.conflict` |
| 416 | Re-query `nextExpectedRanges` and continue |
| 429, 503 with `Retry-After` | Pause the **whole queue** until `now + Retry-After` (F-ST-2). Record `storage.throttled` INFO |
| 500, 502, 504, timeouts, connection errors | RETRY_WAIT with backoff `30 s × 2^attempts` (cap 60 min, ± 20% jitter). After 24 h of attempts: FAILED + alert (F-ST-5). Raise WARNING `storage.offline` when no Graph call has succeeded for 10 min (F-ST-1) |
| 507 | BLOCKED_QUOTA (whole queue) + CRITICAL `storage.quota` (F-ST-4) |
| Any other 4xx | FAILED + alert with the response body (truncated to 2 KB) |

**Queue-wide blocks:**

- An `OPS_BLOCK` row in `alerts` holds `BLOCKED_AUTH` or `BLOCKED_QUOTA`.
- The queue does not run while a block is active, except for one probe operation every 15 min. A successful probe clears the block automatically.
- The Super Admin can also click "Retry now".

## 4. Operations queue (`storage_operations`; job `storage.process_queue`, continuous)

1. Claim: `SELECT … WHERE state IN ('PENDING','RETRY_WAIT') AND next_attempt_at <= now() ORDER BY priority, created_at FOR UPDATE SKIP LOCKED LIMIT 1`, then set `state = RUNNING` and commit.
    - Priority 0 `SHIP_WAL` (ordered by segment), 1 `UPLOAD_BACKUP_OBJECT`, 2 `REPAIR_FILE`, 3 `ARCHIVE_FILE`.
2. Process with GraphProvider, outside any DB transaction.
3. Finish: in one transaction, set the operation to DONE and call the **completion handler** registered for its `kind`. For example, `files.on_archived(op, ref)` sets the blob and version to ARCHIVED and publishes `file.archived`; `backup.on_wal_shipped(op, ref)` records `backup_objects`.
4. **Crash recovery:** on worker start, any RUNNING operations with `updated_at` older than 10 min are reset to RETRY_WAIT. Upload state (`uploadUrl`, `confirmed_bytes`) survives, so the upload resumes.
5. One operation at a time (memory and bandwidth). Throughput target: at least 40 Mbps when unthrottled.

## 5. Reconciliation

| Job | Specification |
| --- | --- |
| `storage.delta_reconcile` (02:00) | For each library, call `changes(cursor)` until it is complete; store the new `deltaLink` in `storage_cursors`. For each changed item matched to a known `item_id`: **deleted** → set the blob or backup object to MISSING, CRITICAL `storage.missing` (F-FL-5), and if the local cache has the blob, enqueue `REPAIR_FILE`, which re-uploads under the **same name** with a `.restored-<date>` suffix if the name is still blocked; **renamed or moved** → record `storage.drifted` INFO and update the stored path (identity is `item_id`); **content changed (etag and hash differ)** → DRIFTED + CRITICAL. If the cursor has expired (410), do a full inventory |
| `storage.orphan_sweep` (02:30) | Items in Business-Files with no matching `blobs.item_id`: if a pending `ARCHIVE_FILE` operation has the same `remote_path`, adopt the item (F-FL-4). Otherwise move it to `/_quarantine/` (a PATCH of `parentReference`, never a delete) + WARNING |
| `storage.full_inventory` (1st of month, 03:00, heavy) | Page through every item; compare with the DB; spot-check the hash on 2% of items (stream the download, compute, discard) |
| `storage.capacity` (hourly) | `usage()` on the site drive quota. Thresholds 70 / 80 / 90 / 95% → WARNING, WARNING, CRITICAL, CRITICAL. At ≥ 95%: BLOCKED_QUOTA for `ARCHIVE_FILE` only. WAL and backups continue into the remaining space, because recovery data takes priority |

## 6. Remote naming

| Library | Path |
| --- | --- |
| Business-Files | `Files/{YYYY}/{MM}/{file_code}` |
| System-DR | `PostgreSQL/WAL/{timeline}/{segment}.zst.age` · `PostgreSQL/Base-Backups/{YYYY}/{MM}/{DD}/base.tar.zst.age` · `Daily-Logical/{YYYY}/{MM}/bkp-{YYYYMMDD}-2300/{artefact}.age` · `Manifests/{YYYY}-{MM}.jsonl` · `Recovery-Documentation/` (a copy of `22_RUNBOOKS.md`, uploaded on deploy) |

**Monthly manifest:** `files.on_archived` appends `{file_code, sha256, size, drive_id, item_id, archived_at}` to the local `manifest-YYYY-MM.jsonl`. A daily job uploads it as `UPLOAD_BACKUP_OBJECT`, replacing the previous upload of the same month. **This is the one allowed replace, and it applies to the DR library only.** Its purpose is to make the archive self-describing without the database.

## Steps and prompts

### M05.1 — Provider interface + fake provider
```
Read CLAUDE.md, docs/plan/01_ARCHITECTURE_CONTRACT.md and docs/plan/M05_STORAGE_ONEDRIVE.md. Implement Step M05.1:
- ots/storage/provider.py exactly per section 1;
- ots/storage/fake.py, a LocalDirProvider backed by a directory, which simulates conflicts (409), size reporting, a delta cursor, quota and injectable failures (a configurable sequence of status codes), for tests and dev;
- settings switch OTS_STORAGE_BACKEND=graph|fake.
Tests: the fake honours conflict=fail; delete_dr refuses the BUSINESS library.
```

### M05.2 — GraphProvider (auth, small/large upload, download, head, find, usage)
```
Implement Step M05.2: ots/storage/graph.py per section 2, including the certificate-based msal auth, the token cache, the small and large upload paths with persisted uploadUrl (encrypted) and confirmed_bytes, resume via nextExpectedRanges, never sending auth headers to uploadUrl, the bandwidth token bucket by IST hours, streaming download, and ots/storage/quickxor.py.
Tests with respx:
- full large-upload happy path;
- resume after a simulated crash at piece 3;
- 416 handling;
- size mismatch → VerificationFailed;
- QuickXorHash against fixed vectors (generate the vectors once from a real Graph upload during the P0 gate and commit them to tests/storage/vectors.json).
```

### M05.3 — Error classifier + queue-wide blocks
```
Implement Step M05.3: classify() per section 3 as one pure function with a table-driven test covering every row; the queue-wide block/probe mechanism; and the alert keys storage.auth, storage.quota, storage.offline, storage.throttled, storage.conflict and storage.cert_expiring.
Failure tests: tests/failure/test_F_ST_1.py (offline 10 min → WARNING; recovery resolves), test_F_ST_2.py (429 Retry-After 120 → the whole queue pauses 120 s, frozen clock), test_F_ST_3.py (403 → BLOCKED_AUTH; probe every 15 min; success clears it), test_F_ST_4.py (507 → BLOCKED_QUOTA).
```

### M05.4 — Operations queue + completion handlers
```
Implement Step M05.4: the storage_operations model, the storage.process_queue job with the SKIP LOCKED claim, priorities, the RUNNING-stale reset at start-up, and the completion-handler registry (kind → callable run inside the finishing transaction).
Failure tests: test_F_ST_5.py (24 h of 5xx → FAILED, dead letter, alert, local file pinned), test_F_ST_6.py (uploadUrl 404 mid-upload → adopt if complete, else restart), test_F_ST_7.py (409 with matching hash → adopt; mismatch → FAILED + CRITICAL).
Two-worker test: no operation is processed twice.
```

### M05.5 — Delta reconciliation
```
Implement Step M05.5: storage.delta_reconcile per section 5, with storage_cursors, the 410 → full-inventory fallback, MISSING/DRIFTED handling, and REPAIR_FILE enqueueing.
Failure test: test_F_FL_5.py (delete an archived item in the fake provider → next run marks it MISSING, raises CRITICAL and repairs it from cache).
```

### M05.6 — Orphan sweep + quarantine
```
Implement Step M05.6: storage.orphan_sweep per section 5 (adopt or quarantine; never delete).
Failure test: test_F_FL_4.py (simulate upload success, then DB commit failure → next sweep adopts the item; the version reaches ARCHIVED; no duplicate remote items).
```

### M05.7 — Capacity + certificate monitoring
```
Implement Step M05.7: the storage.capacity job (thresholds, BLOCKED_QUOTA for ARCHIVE_FILE only at ≥ 95%, DR continues), storage_capacity history rows, and the certificate expiry check.
Tests: thresholds; at 96% WAL ships but files queue.
```

### M05.8 — Full inventory + manifest
```
Implement Step M05.8: storage.full_inventory (heavy, monthly, 2% hash spot-check, streamed) and the monthly manifest append + daily upload to DR:Manifests (the only allowed replace, DR library only; enforce it in code with an allowlist).
Tests: inventory detects an injected size mismatch; the manifest line format.
```

## M05 done-gate

- [ ] Against the real tenant:
  - a 200 MB file archives and verifies;
  - unplugging the internet for 30 min queues everything, and it drains after reconnect;
  - deleting a file in the SharePoint web UI is detected the next day and repaired.
- [ ] Every F-ST and F-FL-4/5 test is green.
