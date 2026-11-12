# M11 — Work History, Calendars, Search, Reports

**Depends on:** M06, M07, M08.
**Produces:**

- the employee and Master calendars (FullCalendar);
- employee work-history pages;
- global search;
- reports with CSV export.

**Failure IDs:**

- F-RP-1: a report over a large range must not exhaust memory.
- F-RP-2: reports for past periods use the historical hierarchy.

## 1. Calendars

- FullCalendar 6 is vendored and loaded **only** on calendar pages. Views: day, week, month.
- Feed endpoint: `GET /history/calendar/events?user=&start=&end=` returns JSON.
    - Assignments are placed by `scheduled_start_at` (or `assigned_at`) through `due_at`. The colour follows the status badge mapping. Overdue assignments get a red outline.
    - Optional overlays: attendance status per day, as a background event.
    - Range limit: 62 days per request. Paginated internally.
- Clicking an event opens the task detail drawer. Clicking a day header opens the M06 attendance day drawer.
- Master view: choose an employee (scoped to subtree), or "All (subtree)" with one resource row per person (FullCalendar resource timeline is a premium plugin, so instead render a list grouped by person for the week).

## 2. Work history page (profile → Work tab)

- **Summary cards:**
    - assigned / completed / in progress / awaiting review / overdue counts;
    - average time from assignment to submission;
    - blocked hours.
- **History table:** cursor-paginated assignments with filters for date range, status, priority, task title, has files, assigned by and late.
- **Timeline sub-tab:** the M03 activity timeline for the selected range.

## 3. Global search (`/search`)

- One box. Results are grouped as Tasks (tsvector + trigram), Files (M08 search), and People (Super Admin and Sub-Admin only; name, employee ID, designation).
- Filters as on the history table.
- Permission-scoped.
- Target: under 500 ms on 10k tasks and 100k file versions.

## 4. Reports (`/reports`, Super Admin and Sub-Admin scoped)

| Report | Content | Notes |
| --- | --- | --- |
| Workload | Per person: open assignments by status, overdue, due in the next 7 days | Current hierarchy |
| Completion | Per person per period: assigned, approved, average cycle time, % submitted late, change-request count | Period hierarchy via `reporting_history` (F-RP-2) |
| Blocked time | Per task, per person and per manager: blocked hours, split by `block_reason` | Shows where missing inputs cause delay |
| Attendance | Per person per month: P / L / A / WO / H days, active hours, idle logouts, corrections | Uses the effective status |
| Files | Uploads per person per period, storage by state, queue backlog | Super Admin only |

- Rendered as tables (M01 table component, right-aligned numbers).
- **CSV export** is streamed with `StreamingHttpResponse` using a server-side cursor (`.iterator(chunk_size=2000)`) (F-RP-1).
- Date-range limit: 24 months per request.

## Steps and prompts

### M11.1 — Calendar feed + pages
```
Read CLAUDE.md and docs/plan/M11_HISTORY_REPORTS.md. Implement Step M11.1: the calendar events endpoint (62-day cap, status colours, overdue outline, attendance background overlay), the employee calendar page, the master calendar with an employee selector and a grouped-by-person week list.
Tests: range cap; permission scope; query count ≤ 4 per feed call.
```

### M11.2 — Work history + summary metrics
```
Implement Step M11.2: the profile Work tab (summary cards, filtered history table, timeline sub-tab).
Tests: metric correctness on a seeded dataset; cursor pagination stability.
```

### M11.3 — Global search
```
Implement Step M11.3: /search with grouped results and filters.
Performance test (@slow): seed 10k tasks and 100k versions; p95 < 500 ms over 50 random queries.
```

### M11.4 — Reports + streaming CSV
```
Implement Step M11.4: the five reports in section 4 and the streamed CSV.
Tests: tests/failure/test_F_RP_1.py (export 500k synthetic rows; peak RSS increase < 60 MB); test_F_RP_2.py (completion report for last month attributes to the old manager after a manager change).
```

## M11 done-gate

- [ ] Search meets the performance target.
- [ ] Reports reconcile with hand-counted data for one employee and one month.
