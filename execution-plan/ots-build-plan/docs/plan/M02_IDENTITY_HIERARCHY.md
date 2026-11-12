# M02 — Identity, Profiles and Reporting Hierarchy

**Depends on:** M00, M01.
**Produces:**

- `users` (custom user model), `companies`, `departments`, `reporting_history`;
- login (password stage; the selfie stage is added in M06);
- lockout, break-glass;
- profile CRUD (Super Admin), org chart;
- permission policies for people.

**Failure IDs:** F-ID-1, F-ID-2, F-ID-3, F-ID-4, F-HR-1, F-HR-2.

## Rules

1. Custom user model `accounts.User` (set `AUTH_USER_MODEL` before the first migration). Columns per contract section 6. `login_id` is the `USERNAME_FIELD`, with unique `lower(login_id)`.
2. Roles: `SUPER_ADMIN`, `SUB_ADMIN`, `EMPLOYEE`. `DISPLAY` is not a user row (see M10).
3. **Only SUPER_ADMIN creates, edits or deactivates accounts.**
4. `reports_to`:
    - required for every role except `SUPER_ADMIN`;
    - must point to an **active** user of **higher rank** (`SUPER_ADMIN` > `SUB_ADMIN` > `EMPLOYEE`);
    - an `EMPLOYEE` may report to a `SUB_ADMIN` or directly to the `SUPER_ADMIN`.
5. Loop prevention: a DB trigger `users_no_cycle` uses a recursive CTE from `NEW.reports_to` upwards and raises if it reaches `NEW.id`. The service layer validates first, so the user gets a friendly message.
6. A user with active reports cannot be deactivated or demoted until the reports are moved (the service raises `ValidationFailed` and lists their names).
7. **The last active SUPER_ADMIN** (excluding break-glass) cannot be deactivated or demoted (F-ID-2). DB constraint trigger + service check.
8. Every `reports_to` change, in the same transaction:
    - closes the current `reporting_history` row (`effective_to = today`);
    - opens a new one;
    - writes `audit_log` action `user.manager_changed`.
9. Subtree query: `selectors.subtree_ids(manager_id)` uses a recursive CTE over current `reports_to`. `selectors.subtree_ids_at(manager_id, date)` uses `reporting_history` (for reports about past dates).
10. **Login** (`/login`):
    - Argon2id check.
    - 5 failures within 15 min set `locked_until = now + 15 min` (F-ID-4); return 423 with the minutes remaining.
    - Every attempt is written to `audit_log` (`auth.login_failed` / `auth.login_ok`) with `client_ip`.
    - On success with `must_change_password`, force the change-password page.
11. Passwords: at least 10 characters, not equal to the login ID, not in a bundled list of the 10,000 most common passwords. A reset by the Super Admin sets `must_change_password`.
12. **Break-glass:**
    - `bootstrap_admins` creates the primary Super Admin and `breakglass` (`is_break_glass=True`, SUPER_ADMIN, random 24-character password printed once for sealing).
    - Break-glass login raises CRITICAL alert `auth.breakglass_used`.
    - `manage.py reset_master --login <id>` (server shell only) sets a temporary password, writes audit, raises CRITICAL alert.
13. Sessions: Django DB sessions; cookies `HttpOnly`, `Secure`, `SameSite=Lax`. The idle and attendance session logic arrives in M06. `SESSION_COOKIE_AGE = 12h` as an absolute ceiling.
14. Profile photo (`reference_photo_path`): uploaded by the Super Admin; JPEG or PNG at most 2 MB; re-encoded to 480×480 JPEG; stored in `/srv/data/selfies/reference/`.

## Permission policies (registered in `accounts/permissions.py`)

| Action | SUPER_ADMIN | SUB_ADMIN | EMPLOYEE |
| --- | --- | --- | --- |
| `user.create`, `user.update`, `user.deactivate`, `user.set_manager`, `user.reset_password` | ✓ | ✗ | ✗ |
| `user.view` | all | own subtree + self | self |
| `org.view_chart` | full tree | full tree, but names only outside own subtree | ✗ (sees own manager only) |
| `audit.view` | all | subtree's records | own login records |

## Screens

- **People list:** table with name, employee ID, designation, department, manager, role, status and today's attendance dot (filled later by M06). Filters: role, department, manager, active.
- **Create/edit person:** a form grouped as Identity / Position / Education / Work pattern / Contact. The manager dropdown lists only valid managers (active, higher rank).
- **Profile page:** header card (photo, designation, qualification, reports-to), then tabs: Work (M07), Attendance (M06), Files (M08), Activity (M03).
- **Org chart:** collapsible tree of rounded person cards; lazy-load children with HTMX.
- **Change password, Login.**

## Steps and prompts

### M02.1 — Models, constraints, triggers

```
Read CLAUDE.md, docs/plan/01_ARCHITECTURE_CONTRACT.md and docs/plan/M02_IDENTITY_HIERARCHY.md. Implement Step M02.1.
Create the custom User model (AUTH_USER_MODEL), Company, Department and ReportingHistory with the exact columns and enums from the contract.
Add DB-level enforcement:
- check constraint: reports_to IS NOT NULL unless role = 'SUPER_ADMIN';
- trigger users_no_cycle (recursive CTE);
- trigger users_last_super_admin preventing deactivation or demotion of the last active non-break-glass SUPER_ADMIN;
- unique index on lower(login_id) and unique employee_id.
Tests: tests/failure/test_F_ID_1.py (duplicate login_id differing only in case is rejected), test_F_ID_2.py, test_F_HR_1.py (cycle and inactive or lower-rank manager are rejected at the DB level, bypassing services with raw SQL).
```

### M02.2 — Services and selectors

```
Implement Step M02.2: accounts/services.py with create_user, update_profile, set_manager (writes reporting_history + audit atomically), deactivate_user (blocked while reports exist; lists them), reset_password and change_password; accounts/selectors.py with subtree_ids and subtree_ids_at (recursive CTE).
Every service checks permissions via ots.core.permissions.require and publishes events user.created, user.updated, user.manager_changed and user.deactivated.
Tests: rank rules; manager change history (test_F_HR_2.py: change manager on day D; subtree_ids_at(old_manager, D-1) still includes the user); query count of subtree_ids is 1.
```

### M02.3 — Login, lockout, break-glass

```
Implement Step M02.3: the password login view (selfie stage stubbed behind a feature flag that M06 will enable), the lockout (5 failures / 15 min → 423), audit records for every attempt, the must_change_password flow, the password policy (min 10, not login_id, top-10k list bundled in accounts/data/common_passwords.txt), and the bootstrap_admins and reset_master management commands with CRITICAL alerts.
Tests: tests/failure/test_F_ID_4.py (lockout and auto-unlock with freezegun); break-glass login raises the alert; reset_master writes audit.
```

### M02.4 — Permission policies and role-escalation defence

```
Implement Step M02.4: register every policy in the M02 permission table. Add the scoped manager User.objects.visible_to(user).
Tests: a parametrised matrix test over every (role × action) cell; tests/failure/test_F_ID_3.py (an EMPLOYEE POSTing to the user-update endpoint with role=SUPER_ADMIN gets 403 plus an audit row 'security.denied').
```

### M02.5 — Screens

```
Implement Step M02.5: people list, create/edit form, profile page shell with tabs, org chart (HTMX lazy children), change password. Use only M01 components. The manager dropdown is server-filtered to valid managers. Every form submit sends an Idempotency-Key (from the hx-headers helper in base.html).
Tests: view permission tests per role; the org chart renders 3 levels; list view query count ≤ 6 for 50 users.
```

## M02 done-gate

- [ ] F-ID-1 to F-ID-4, F-HR-1 and F-HR-2 are green.
- [ ] Super Admin, Sub-Admin and Employee accounts can log in from 3 different PCs.
- [ ] Manager-first creation is enforced.
