# M09 — PWA and Offline Synchronisation

**Depends on:** M00.5 (idempotency), M07, M08, M10.1 (SSE).
**Produces:**

- an installable PWA (manifest + service worker);
- the offline app shell;
- an IndexedDB durable outbox with ordered send;
- conflict UI;
- the pending chip;
- draft autosave.

**Failure IDs:** F-SY-1 to F-SY-4, plus F-SY-5 (duplicate send across tabs).

## 1. Installability

- `/manifest.webmanifest`:
    - `name` "Office Task System", `short_name` "OTS";
    - `display` standalone, `start_url` `/app/`, `scope` `/`;
    - `background_color` and `theme_color` `#FFFFFF`;
    - icons at 192 and 512 px (maskable).
- **Service worker `/sw.js`** (served from the root scope, with `Cache-Control: no-cache`):
    - **App shell cache** (versioned by release SHA): CSS, JS, fonts, icons, the offline page and the last-rendered shells of the dashboard and My tasks (stale-while-revalidate for those HTML fragments).
    - **Never cache:** authenticated file downloads, selfies, `/attendance/*`, any POST.
    - **Navigation fallback** when the server is unreachable: the `/offline` shell, which shows cached "My tasks" data from IndexedDB and the pending chip.
- An install prompt button appears in the user menu.
- Onboarding instruction: "Install app" + "Open at login":
    - Windows: shell:startup shortcut created by the install script.
    - macOS: Login Items.

## 2. Outbox (IndexedDB database `ots`, store `outbox`)

**Record:**

```
{event_id (UUIDv7, also the Idempotency-Key), user_id, session_id, device_id, type, entity_id,
 base_version, method, url, body, client_observed_at, state, attempts, last_error, created_at}
```

**Rules:**

1. All business writes (task transitions, comments, end-of-day closure, idle/active posts, handovers) go through `outbox.send(cmd)`.
2. `send` writes the record as PENDING **first**, then attempts delivery immediately.
3. Exactly one **sender loop** per origin, using the Web Locks API `navigator.locks.request('ots-outbox', …)` so several tabs never double-send (F-SY-5).
4. Sending is strictly in `created_at` order. The next record is not sent until the previous one is ACKNOWLEDGED, CONFLICT or REJECTED.
5. Server responses:
    - 2xx (including `Idempotent-Replay`): ACKNOWLEDGED.
    - 409 with `{current}`: CONFLICT. Show the conflict card.
    - 422 or 403: REJECTED. Show the error and stop retrying.
    - 401: hold. Show the login overlay. After re-login, resume **only if the user_id matches**; otherwise keep the records, flag them, and show "Pending changes belong to another user" (F-SY-4).
    - Network error or 5xx: retry with backoff 2, 4, 8 … up to 60 s. Retry immediately on the `online` event, on page focus and on SSE reconnect.
6. **Offline-allowed types** when the server is unreachable (F-SY-1):
    - own assignment transitions `start`, `pause_eod`, `resume`, `block`, `submit` (note-only; or a file ≤ 25 MB, stored as a Blob in the `files` store, total ≤ 200 MB per device);
    - `task.comment`;
    - the end-of-day batch;
    - `attendance.idle`.

    Everything else is disabled in the UI while offline, with a tooltip.
7. **Storage durability (F-SY-3):**
    - Call `navigator.storage.persist()` on first login and record the result. If it is refused, show a one-time notice.
    - Check `navigator.storage.estimate()` daily: warn at 80%; at 95%, refuse new offline file submissions.
8. **Pending chip:** shows the count of records not in a final state. Clicking opens a list with per-item state and "Retry" / "Discard" (discard needs confirmation and is audited on the server when next online, as `sync.discarded`).
9. `beforeunload` warns if the pending count is above 0.
10. ACKNOWLEDGED records are deleted after 24 h. CONFLICT and REJECTED records are kept until the user acts.

## 3. Server side

- Every outbox-capable endpoint:
    - accepts `base_version`;
    - stores `client_observed_at` as information only;
    - flags the resulting event payload `offline_synced=true` if `client_observed_at` is more than 2 min before the server's receipt time.
- Conflict responses include the current serialized state and the human message "Changed by {name} at {time}".
- Duplicates are handled by M00.5.

## 4. Drafts

- Comment boxes, the create-task form and the end-of-day closure modal autosave to IndexedDB store `drafts` (key = form ID + entity ID) every 2 s while typing, and restore on reopen.
- A draft is cleared on successful submission.
- Drafts are never sent automatically.

## Steps and prompts

### M09.1 — Manifest, service worker, offline shell
```
Read CLAUDE.md and docs/plan/M09_PWA_SYNC.md. Implement Step M09.1: the manifest, sw.js with a versioned shell cache (release SHA injected at build), the never-cache rules, the /offline shell, the install button, and deploy/scripts/pwa-autostart.ps1 + a macOS instructions page.
Playwright test: load the app, go offline (context.setOffline), navigate → offline shell renders with the cached My tasks.
```

### M09.2 — Outbox library
```
Implement Step M09.2: static_src/js/outbox.js per section 2:
- IndexedDB schema with versioned upgrades;
- send();
- the single sender via navigator.locks;
- ordered delivery and the state machine;
- backoff;
- 401 hold with the user_id check;
- persist() and estimate() handling;
- the pending chip component;
- the beforeunload guard.
Route all business writes from M06, M07 and M08 through outbox.send. Keep the plain fetch path only for login and uploads > 25 MB.
Unit tests with fake-indexeddb (vitest, run in the assets build stage); Playwright tests:
- test_F_SY_1 (server down → actions queue; server up → delivered in order, flagged offline_synced);
- test_F_SY_2 (stale base_version → conflict card);
- test_F_SY_4 (401 → re-login as the same user resumes; as a different user, held);
- test_F_SY_5 (two tabs → no double send; server sees each key once).
```

### M09.3 — Offline-allowed actions + file submissions
```
Implement Step M09.3: UI gating of offline-allowed vs disabled actions; offline file submissions ≤ 25 MB stored as Blobs (200 MB device cap); on reconnect, upload via the M08 resumable protocol, then send the dependent submit transition only after the file reaches STAGED (chain within the outbox).
Test test_F_SY_3 (quota at 95% → offline file submission refused with a message).
```

### M09.4 — Drafts
```
Implement Step M09.4: draft autosave/restore per section 4.
Playwright: type a comment, close the tab, reopen → the draft is restored; submit → the draft is cleared.
```

## M09 done-gate

- [ ] Acceptance tests 32 and 33 pass (see `23_RELEASE_QUALIFICATION.md`).
- [ ] The PWA is installed and auto-starts on one Windows and one macOS machine.
