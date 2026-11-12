# M06 — Attendance, Selfie Login and Work Sessions

**Depends on:** M02 (login), M03, M10.1 (SSE endpoint skeleton; build M10.1 first if it is not already built, it is small).
**Produces:**

- the selfie step at login;
- `work_sessions`;
- server-enforced idle logout;
- session end reasons;
- `attendance_days` roll-up;
- the green/red calendar and day drawer;
- corrections;
- holidays;
- a timeline provider for M03.

**Failure IDs:**

- F-AT-1: browser closed or PC crashed.
- F-AT-2: idle code in the browser stalls.
- F-AT-3: camera unavailable.
- F-AT-4: device clock wrong.
- F-AT-5: server restart.
- F-AT-6: duplicate login event.
- F-AT-7: selfie upload fails.

## 1. Login flow (two stages)

1. **Stage 1:** password (M02.3). On success, create a **pending login** (signed, server-side, valid 3 min). No session exists yet.
2. **Stage 2, selfie:**
    - The page asks for the camera (`getUserMedia({video:{width:640,height:480}})`). This requires HTTPS.
    - The user presses Capture. The browser draws to a canvas, scales to 480×360, encodes JPEG at quality 0.7 (about 35 KB) and POSTs with an `Idempotency-Key`.
    - The server validates: JPEG magic bytes, at most 200 KB, dimensions 480×360 ± 10%. It re-encodes with Pillow (stripping EXIF) and stores `/srv/data/selfies/YYYY/MM/DD/<session_id>.jpg` with mode 0640.
    - Camera denied or missing (F-AT-3): show "Camera required for attendance. Contact the administrator." Login does not complete, and `auth.selfie_unavailable` is audited.
    - Upload fails (F-AT-7): the client retries 3 times with backoff while the pending login is still valid. After that, the login fails cleanly; nothing is half-created.
3. **One transaction:**
    - create the Django session;
    - create the `work_sessions` row (`login_at = now()`, `selfie_path`, `client_ip`, `user_agent`, `device_id` from a localStorage UUID, `work_date = today_local()`);
    - write `audit auth.login_ok`;
    - publish event `session.started`.
4. Duplicate submissions (F-AT-6) replay through the idempotency layer, so exactly one session is created.
5. After login the PWA requests the Idle Detection permission once (`IdleDetector.requestPermission()` must come from a user gesture; use a "Enable attendance tracking" button on first login per device).
    - Granted: `idle_mode = OS`.
    - Not granted: `idle_mode = TAB_ONLY`. Activity listeners for mousemove, keydown and visibilitychange stay inside the tab, and the session is flagged on manager views.

## 2. Liveness and idle (no heartbeat)

| Signal | Client sends | Server writes |
| --- | --- | --- |
| Live connection | The page keeps an SSE connection (M10) | On connect: `live_connected = true`. On disconnect: `live_disconnected_at = now()` (written by the SSE view's `finally` block) |
| Idle starts | `IdleDetector` change to `userState=idle` or `screenState=locked` (threshold 60 s minimum, as the API requires) → POST `/attendance/idle` `{state:"idle"}` | `idle_started_at = now() − 60 s` (backdated by the detector threshold), `active_resumed_at = null` |
| Activity resumes | change to `active` and `unlocked` → POST `{state:"active"}` | `active_resumed_at = now()`. If the idle span is under 45 min, add the idle time to an `idle_seconds_total` field. Active time is computed later |
| Logout | Button "Log out for the day" (runs the M07 end-of-day closure first) or "Log out (break)" | `logout_reason = END_OF_DAY` or `MANUAL` |

**Server sweep `attendance.idle_sweep` (every 60 s):** sessions with `logout_at IS NULL` are closed when either of these is true:

1. `idle_started_at IS NOT NULL AND active_resumed_at IS NULL AND now() − idle_started_at ≥ 45 min`. Close with `IDLE_45`, `logout_at = idle_started_at + 45 min` (F-AT-2: the server enforces this even if the browser's code is dead).
2. `live_connected = false AND now() − live_disconnected_at ≥ 2 min`. Close with `BROWSER_CLOSED`, `logout_at = live_disconnected_at` (F-AT-1).
3. TAB_ONLY sessions: the browser sends `{state:"idle"}` after 45 min with no in-tab activity. The same rule 1 applies, so the threshold is 0 extra minutes.

In both cases, delete the Django session, publish `session.ended` (the SSE handler pushes a "logged-out" message so the open page shows the logged-out screen), and audit the event.

**Server restart (F-AT-5):**

- Sessions are database rows, so they survive.
- On app start-up, mark all `live_connected = false` with `live_disconnected_at = boot_time`.
- Clients reconnect their SSE within 30 s and set `live_connected = true` again, so the 2-minute rule never fires for a short restart.

**Clock (F-AT-4):** all times come from the server. The client's time is ignored.

`active_seconds` is computed at close: `(logout_at − login_at) − idle_seconds_total`, where an open idle span counts up to `logout_at`.

**FORCED:** the Super Admin can end any session from the People or Attendance screen (reason required, audited).

## 3. Attendance days

- **`attendance.rollup` (22:45 daily, and on demand for "today" when the calendar loads):** for each active user, for `today_local()`:
    - `WO` if the weekday is in `weekly_off`.
    - `H` if the date is in `holidays`.
    - `P` if there is ≥ 1 session; `L` if the first login is after `shift_start + OTS_LATE_GRACE_MINUTES`.
    - `A` otherwise.
    - Also `first_login`, `last_logout`, `active_minutes`, `sessions`, `idle_logouts`, `late_minutes`.
    - Upsert. Re-running is idempotent.
- **Corrections:**
    - A Sub-Admin requests one for their subtree; the Super Admin approves it.
    - Approval inserts an `attendance_corrections` row and re-runs the roll-up for that user and date.
    - The **effective status** is the latest approved correction, else the roll-up.
    - The calendar shows a small marker on corrected days. The original roll-up data stays in the day drawer.
- **Holidays:** a CRUD page for the Super Admin, applied per company.

## 4. Screens

- **Attendance tab** on the profile, and "My attendance" for employees. A month grid:
    - green = P;
    - green with an amber dot = L;
    - red = A;
    - grey = WO or H;
    - outlined = today;
    - a correction marker where applicable.
- **Day drawer (38.2% width):**
    - Sessions: login time, selfie thumbnail (Super Admin and self only; viewing writes `selfie.viewed` audit), logout time, reason chip, active minutes.
    - Work: provider-fed from M07 and M08: tasks touched, status changes, uploads.
    - Totals.
- **Team today (Sub-Admin and Super Admin dashboard widget):** who is present, late or absent; live sessions; who is idle now.
- **Session flags:** a TAB_ONLY badge where applicable.

## Steps and prompts

### M06.1 — Models + selfie capture + two-stage login
```
Read CLAUDE.md and docs/plan/M06_ATTENDANCE.md. Implement Step M06.1:
- models work_sessions, attendance_days, attendance_corrections and holidays (contract section 6);
- the pending-login token;
- the selfie page (Alpine component: camera, capture, canvas 480x360 JPEG at 0.7, upload with an Idempotency-Key, 3 retries);
- server validation and re-encode with Pillow (strip EXIF);
- storage path and modes;
- an atomic session + work_session creation;
- audits and the session.started event.
Enable the M02 selfie feature flag.
Failure tests: test_F_AT_3.py (no selfie → no session), test_F_AT_6.py (same key twice → one work_session), test_F_AT_7.py (upload fails 3x → clean failure, no rows).
```

### M06.2 — Idle endpoints, live-connection tracking, server sweep
```
Implement Step M06.2:
- POST /attendance/idle {state} (idle backdated 60 s);
- live_connected / live_disconnected_at maintained by the SSE view hooks (M10.1);
- the attendance.idle_sweep job with the exact rules in section 2, including FORCED;
- the start-up reset of live_connected;
- session.ended event → SSE "logged-out" message;
- the active_seconds computation.
Failure tests (freezegun): test_F_AT_1.py (disconnect → BROWSER_CLOSED at the disconnect time after 2 min), test_F_AT_2.py (idle with no resume for 45 min → IDLE_45, logout_at = idle_start + 45), test_F_AT_4.py (client time in the payload is ignored), test_F_AT_5.py (restart within 30 s → no closure).
```

### M06.3 — Client idle detection module
```
Implement Step M06.3: static_src/js/attendance-idle.js.
- If 'IdleDetector' is in window and permission is granted: threshold 60000; on change post idle/active.
- Else: TAB_ONLY fallback with a 45-minute in-tab inactivity timer.
- A first-login "Enable attendance tracking" button requests the permission, then records idle_mode via POST.
- Every post carries an Idempotency-Key and uses the M09 outbox once M09 exists (until then, a direct fetch with 3 retries).
Write a Playwright test (tests/e2e/test_idle.py) using Chrome DevTools idle emulation (CDP Emulation.setIdleOverride) to drive idle and active states.
```

### M06.4 — Roll-up, corrections, holidays
```
Implement Step M06.4: attendance.rollup (idempotent upsert, statuses P/L/A/WO/H, late grace from settings), the correction request/approve flow with permissions (Sub-Admin requests for their subtree, Super Admin approves) and the effective-status selector, and holidays CRUD.
Tests: each status rule; a correction keeps the original visible; re-running the roll-up is idempotent; query count of the roll-up for 30 users ≤ 5.
```

### M06.5 — Calendar, day drawer, team-today widget
```
Implement Step M06.5: the month grid (pure server-rendered grid; FullCalendar is NOT needed here), the day drawer with sessions/work/totals (selfie thumbnails permission-checked and audited as selfie.viewed), the team-today widget, and the TAB_ONLY badge. Register the attendance provider in the M03 timeline.
Tests: permissions (an employee sees only themselves; a Sub-Admin sees their subtree; selfies only for SUPER_ADMIN or self); the drawer query count ≤ 8.
```

### M06.6 — Write-count budget
```
Implement Step M06.6: a test tests/attendance/test_write_budget.py simulating a full employee day (login, 2 idle spans, 1 IDLE_45 + re-login, end-of-day logout). Count INSERT/UPDATE statements touching attendance tables via connection.execute_wrapper. Assert ≤ 10, excluding task actions.
```

## M06 done-gate

- [ ] All five end reasons have been observed on 3 real PCs.
- [ ] Working 60 min in Excel with the browser in the background keeps the session open.
- [ ] Leaving the PC for 46 min closes it with IDLE_45.
- [ ] Write budget is ≤ 10 per employee per day.
