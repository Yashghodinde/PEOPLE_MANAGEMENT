# M03 — Audit and Activity

**Depends on:** M00.6 (the `AppendOnly` operation), M02.
**Produces:**

- the `audit_log` table and `audit.record()`;
- the activity timeline component;
- the audit viewer;
- the security-denied hooks.

**Failure IDs:** F-AU-1 (audit write failure rolls back the business change); F-AU-2 (tamper attempt rejected).

## Specification

1. `audit_log` columns are in contract section 6. Apply `AppendOnly('audit_log')`.
2. `audit.record(action, actor, target=None, payload=None, request=None)`:
    - must run inside `transaction.atomic()`, otherwise it raises;
    - fills `client_ip` (from `X-Forwarded-For`, set by Caddy) and `request_id`.
3. Mandatory audit actions:
    - `auth.*`, `user.*`, `security.denied`, `settings.*`, `display.*`;
    - `file.download` and `file.preview` (also written to `file_access_log`);
    - `selfie.viewed`;
    - `attendance.correction_*`;
    - `storage.*` admin actions;
    - `backup.manual_*`;
    - `breakglass.*`.
4. `task_events` is the task-domain history (M07). `audit_log` covers security and administration. The **activity timeline** for a person merges these sources for a date range, using one query per source:
    - `task_events` (actor = person);
    - `work_sessions` (M06);
    - `file_versions` uploads;
    - `file_handovers`;
    - `audit_log` (`auth.*` for that person).
5. **Partitioning:** not now. Add a decision-log note: partition `audit_log` and `task_events` by month when either passes 5 million rows.
6. Viewer (`/audit`): filters for actor, action prefix, date range and target. Cursor pagination, 100 rows per page. CSV export of the filtered result, streamed.

## Steps

### M03.1 — Table, API, enforcement
```
Read CLAUDE.md and docs/plan/M03_AUDIT.md. Implement Step M03.1: the audit_log model with AppendOnly, audit.record() (raises outside atomic), the client IP and request ID capture, and a hook in ots.core.permissions so every PermissionDenied writes security.denied in a separate short transaction (it must still be written although the request fails).
Tests:
- tests/failure/test_F_AU_1.py: a service that writes a business row then calls audit.record with a forced DB error; assert the business row does not exist.
- tests/failure/test_F_AU_2.py: UPDATE and DELETE on audit_log as ots_app raise.
```

### M03.2 — Activity timeline and viewer
```
Implement Step M03.2: audit/selectors.activity_timeline(user_id, date_from, date_to), merging the sources listed in the spec, ordered by server time, paginated by cursor; plus the template component components/timeline.html and the /audit viewer with filters and streamed CSV export.
Sources not yet built (sessions, files) are registered through a pluggable provider list, so M06 and M08 add providers without editing M03.
Tests: merge ordering; the query count is one per provider; permission scoping per the M02 table.
```

## M03 done-gate

- [ ] F-AU-1 and F-AU-2 green.
- [ ] Every M02 action produces an audit row (verified by a test that loops the M02 services).
