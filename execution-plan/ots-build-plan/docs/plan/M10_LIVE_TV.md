# M10 — Live Updates (SSE), Notifications, TV Board

**Depends on:** M00.6 (outbox dispatcher), M02. Build M10.1 before M06.2. Build M10.2 to M10.5 after M07.
**Produces:**

- the SSE endpoint and the per-connection fan-out;
- in-app notifications and deadline reminders;
- the TV board on the Xiaomi FX Pro (Fire OS, Fully Kiosk Browser);
- display pairing.

**Failure IDs:**

- F-TV-1: live connection lost.
- F-LV-1: dispatcher lag.
- F-LV-2: the SSE connection limit.
- F-NT-1: duplicate notification on handler retry.

## 1. SSE endpoint

- `GET /live/stream` is an **async** Django view (ASGI) and is exempt from idempotency.
- **Auth:** a user session, or a display token cookie.
- Per connection:
    - an `asyncio.Queue`, registered in the in-process `Hub` keyed by `user_id`, role, subtree and display flag;
    - on connect: mark `work_sessions.live_connected = true` (users only);
    - send `retry: 5000`, then an `event: hello` carrying the server time and release SHA;
    - a heartbeat comment `: ping` every 25 s;
    - on disconnect (`finally`): `live_connected = false`, `live_disconnected_at = now()`.
- **Hub** (`ots/live/hub.py`) runs in the app process.
    - A single `psycopg` async connection `LISTEN ots_live`.
    - The worker's dispatcher handler for each relevant outbox event calls `pg_notify('ots_live', json)`. The payload is ≤ 7 KB: event type, IDs and audience. It never contains document content.
    - The Hub routes each message to the matching queues by audience rules: user IDs, a Super Admin flag, subtree membership (cached per connection, refreshed on `user.manager_changed`), and `display`.
- **Message types:**
    - `task-changed {task_id}`
    - `assignment-changed {assignment_id, task_id}`
    - `notification {id}`
    - `file-archived {version_id}`
    - `alert {key, severity}` (Super Admin only)
    - `logged-out {reason}`
    - `board-changed`
    - `reload`
- Clients react with HTMX: `hx-trigger="sse:task-changed[detail.task_id=='…']"` re-fetches fragments. **No HTML goes over SSE.**
- **Limits (F-LV-2):** at most 3 connections per user (the oldest is closed) and 100 per process. Beyond that, return 503 with `Retry-After: 30`.
- **Caddy:** `flush_interval -1` (M00.2). Uvicorn: `--timeout-keep-alive 75`.
- **Dispatcher lag (F-LV-1):** if the oldest unpublished outbox event is more than 60 s old, raise WARNING `live.lag`. Clients also re-fetch on SSE reconnect, so state converges even when a message was missed.

## 2. Notifications

- Table `notifications`. Created by event subscribers:
    - `assignment.created` → the assignee.
    - `assignment.blocked` → the reports-to manager + the task creator.
    - `assignment.submitted` → the reviewer(s).
    - `assignment.changes_requested` / `assignment.approved` → the assignee.
    - `task.deadline_changed` → the assignees.
    - `file.handed_over` → the recipients.
    - `alert.raised` (CRITICAL) → Super Admins.
- **Delivery acknowledgement (D-052; receipts are defined in M07 section 7):** when a key task event arrives over
  SSE, the page acks it **immediately** with `POST /app/receipts/ack` (no timer, no batching); after an SSE
  reconnect it fetches what it missed first, then acks. If the sender is still looking at the task, the ✓ becomes
  ✓✓ through the same `task-changed` SSE message. The 15-minute URGENT "not received yet" notification to the
  sender is created by the `notifications.deadline_reminders` job's sibling `notifications.undelivered`
  (every minute; unique per receipt).
- **Idempotent (F-NT-1):** unique `(user_id, kind, source_event_id)`.
- Bell icon with unread count; a list page; mark read / mark all read.
- **`notifications.deadline_reminders`** (every 15 min, 08:00 to 20:00):
    - for open assignments due within 24 h and within 2 h, send one reminder each (unique per assignment + window);
    - for overdue assignments, one reminder per day at 10:00.

## 3. TV board (`/display`)

**Pairing:**

1. The Super Admin opens Settings → Displays → "Pair new display". The server generates a 6-digit code, valid 10 min (`display_pairing_codes`).
2. The TV opens `/display/pair` and the code is entered with the remote.
3. The server creates `display_devices` with a 256-bit random token (only its SHA-256 is stored) and sets cookie `ots_display` with HttpOnly, Secure, SameSite=Strict, Max-Age 365 days.
4. The Super Admin can revoke a display, after which the TV shows the pair screen.
5. Every action is audited.

**Data scope:**

- Only assignments in states ASSIGNED, IN_PROGRESS, PAUSED, BLOCKED, SUBMITTED or CHANGES_REQUESTED, on tasks without `hide_from_display`.
- Fields: employee full name, task title (truncated to 60 characters), due time, priority, status label, overdue flag.
- No descriptions, comments or files.

**Layout:**

- 1920×1080 CSS px with the design tokens scaled ×1.618; minimum font 26 px.
- Header: office name, live clock, date, counts (active / awaiting review / overdue).
- Grid 4 columns × up to 8 rows: one card per employee with active work, showing the name and the most urgent task (overdue first, then due soonest) plus "+N more". Overdue cards get a 2 px `bad` border.
- Bottom strip: approvals today (cleared at midnight).
- **No animations except a 233 ms fade.** No large images. Total page JS under 30 KB.

**Live behaviour:**

1. The board joins the SSE stream as a display.
2. On `board-changed`, it does `hx-get /display/board` (full fragment swap).
3. Fallback: if no message or ping arrives for 60 s, reload the page. If SSE fails to connect 3 times, switch to polling `/display/board` every 30 s, retrying SSE every 5 min.
4. At 03:00 the server sends a `reload` message, and the page does a full reload (F-TV-1).

**Fire TV setup** (Office admin checklist, in `22_RUNBOOKS.md` section OPS-3):

- Ethernet connection.
- Developer options: install unknown apps for Downloader.
- Install Fully Kiosk Browser.
- Start URL `https://<server-ip>/display`.
- Launch on boot; keep screen on; reload on network reconnect.
- Ignore SSL errors (accepted risk: read-only board data).
- Screensaver off; sleep never; MEMC off.

## Steps and prompts

### M10.1 — SSE endpoint + Hub + liveness hooks
```
Read CLAUDE.md and docs/plan/M10_LIVE_TV.md. Implement Step M10.1: the async /live/stream view, the Hub with LISTEN ots_live, audience routing, the hello/ping/retry framing, connection limits, the live_connected/live_disconnected_at hooks for work_sessions (guarded until M06 models exist), and the worker-side publisher that maps outbox events to pg_notify('ots_live'). Test with httpx AsyncClient streaming:
- a user receives only their audience's messages;
- a 4th connection evicts the oldest;
- tests/failure/test_F_LV_1.py (stalled dispatcher > 60 s → live.lag alert);
- test_F_LV_2.py.
```

### M10.2 — Notifications + reminders
```
Implement Step M10.2: the notifications model with unique (user_id, kind, source_event_id); subscribers for each event in section 2; the bell, list and mark-read; the deadline_reminders job with unique windows.
Tests: tests/failure/test_F_NT_1.py (a handler run twice for the same event → one notification); the reminder windows with freezegun.
```

### M10.2b — Delivery acknowledgements (live side)
```
Implement Step M10.2b: the client ack on SSE receipt and after reconnect (fetch missed first, then ack), the SSE message that turns the sender's tick into two ticks, and the notifications.undelivered job (urgent tasks, 15 minutes, one notification per receipt).
Tests: an SSE-connected recipient acks within a second of the event; a recipient who reconnects after 10 minutes fetches, then acks; the undelivered job sends exactly one notification per receipt (freezegun).
```

### M10.3 — HTMX live wiring
```
Implement Step M10.3: SSE-driven fragment refresh on the task list/detail, dashboards, notification bell, file off-site badges, and the Super Admin alert banner. The logged-out message redirects to /login with a reason banner. On SSE reconnect, re-fetch the visible fragments.
Playwright: two browsers; an employee changes status → the manager's task detail updates in ≤ 3 s.
```

### M10.4 — Display pairing + board
```
Implement Step M10.4: display pairing (codes, token hash, cookie, revoke, audit), the /display page and /display/board fragment with the data scope and layout in section 3, the reload and polling fallback logic (vanilla JS < 30 KB total), and the 03:00 reload message job.
Tests: a revoked token → pair screen; the board never contains a description or comment text (assert on the rendered HTML with seeded secrets in the description); the fallback timers with a fake clock (vitest).
```

### M10.5 — TV qualification checklist
```
Write docs/qualification/tv_checklist.md with the Fire TV setup and these tests:
- assignment → card within 3 s;
- server restart → recovery ≤ 60 s;
- pull the Ethernet for 2 min → recovery;
- 24 h soak with no memory growth (observe Fully Kiosk's memory stats).
No code changes unless a defect is found.
```

## M10 done-gate

- [ ] The board updates within 3 s.
- [ ] It recovers on its own after a server restart and after a network pull.
- [ ] F-TV-1, F-LV-1, F-LV-2 and F-NT-1 are green.
