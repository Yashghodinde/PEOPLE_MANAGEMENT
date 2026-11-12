# M13 — Security Hardening and Privacy

**Depends on:** all functional modules.
**Produces:**

- security headers and the Content Security Policy;
- rate limits;
- selfie retention;
- privacy notice acknowledgement;
- the security test suite.

**Failure IDs:**

- F-SC-1: cross-user access attempt.
- F-SC-2: path traversal.
- F-SC-3: CSRF.
- F-SC-4: selfie retention.
- F-SC-5: a secret in a backup.

## 1. HTTP

- **Headers:**
    - `Strict-Transport-Security: max-age=31536000` (LAN only, internal CA);
    - `X-Frame-Options: DENY`;
    - `Referrer-Policy: same-origin`;
    - `Permissions-Policy: camera=(self), idle-detection=(self), geolocation=()`.
- **CSP:** `default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'`.
    - Alpine needs no `unsafe-eval` if you use the CSP build of Alpine. Vendor `@alpinejs/csp`.
- **CSRF:** Django middleware. HTMX and the outbox send `X-CSRFToken`.
- **Rate limits** (in-process token buckets, keyed by IP + login_id):
    - login 10/min;
    - selfie upload 10/min;
    - pairing code entry 5/min per IP.

## 2. Files and paths

- Storage paths are built only from SHA-256 hex or UUIDs. A helper `safe_join(base, *parts)` rejects `..`, absolute paths and NUL bytes.
- Uploads are never served from a static route. `X-Content-Type-Options: nosniff` is set on every download.

## 3. Privacy (selfies, attendance)

- **First login after go-live:** show the attendance and selfie policy text (editable by the Super Admin in Settings). The employee clicks "I have read this"; the acknowledgement is stored in `audit_log` as `privacy.acknowledged` together with the policy version.
- **`attendance.selfie_retention`** (05:00): delete selfie files older than `OTS_SELFIE_RETENTION_DAYS` (180), set `work_sessions.selfie_path = null`, and keep the session row. Write an audit summary row with the count deleted.
- Selfie access: Super Admin and self only (M06). Every view is audited.

## 4. Server

- **ufw:** P0.15.
- **SSH:** key-only, `PermitRootLogin no`.
- **Patching:** `unattended-upgrades` for security updates.
- **Docker:** containers run as non-root (except `db`, which uses its own `postgres` user), with `read_only: true` root filesystems where possible (`app`, `worker`: writable tmpfs `/tmp`, data via bind mounts).
- **Secrets:** `/etc/ots`, owner root, mode 600. Mounted read-only.
- **Graph certificate:** rotation procedure in `22_RUNBOOKS.md`, OPS-5.

## Steps and prompts

### M13.1 — Headers, CSP, rate limits
```
Read CLAUDE.md and docs/plan/M13_SECURITY_HARDENING.md. Implement Step M13.1: security headers middleware, the CSP (switch Alpine to the CSP build and fix any inline handlers), and rate limits.
Tests: the headers are present on every response type; a CSP-violation check that fails if any template contains inline <script> or on* attributes (extend scripts/check_templates.py); rate limits return 429.
```

### M13.2 — Security test suite
```
Implement Step M13.2: tests/security/:
- an IDOR sweep (for every detail and download route, request objects belonging to other users for each role → 403/404);
- path traversal against every file route (F-SC-2);
- CSRF missing → 403 (F-SC-3);
- an F-SC-1 consolidated test.
Generate the route list automatically from the URLconf so new routes are covered by default. A route must be explicitly allow-listed to be excluded, with a reason.
```

### M13.3 — Privacy acknowledgement + selfie retention
```
Implement Step M13.3: the policy text setting, the first-login acknowledgement with version, and the selfie retention job.
Tests: tests/failure/test_F_SC_4.py (a 181-day-old selfie is deleted, the session is kept, an audit summary is written); test_F_SC_5.py (generate a nightly set; grep every artefact for each .env value → none found).
```

## M13 done-gate

- [ ] The security suite is green.
- [ ] The CSP has no unsafe directives.
- [ ] The privacy policy is acknowledged by every pilot user.
