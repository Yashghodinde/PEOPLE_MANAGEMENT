"""IMP-09.1 and 09.2: the card landing, its numbers, order, paging, and the pages around it."""

import datetime
import uuid

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from ots.accounts.models import Role
from ots.core.clock import now
from ots.tasks import cards
from ots.tasks.models import AssignmentStatus as S
from ots.tasks.models import Priority, TaskAssignment

from tests.accounts.factories import make_user
from tests.tasks.factories import make_task

pytestmark = pytest.mark.django_db
PW = "correct horse battery"
HOUR = datetime.timedelta(hours=1)


def login(login_id):
    c = Client()
    assert c.post("/login", {"login_id": login_id, "password": PW}).status_code == 302
    return c


@pytest.fixture
def office():
    sa = make_user(Role.SUPER_ADMIN, login_id="boss", full_name="Boss Person")
    m1 = make_user(Role.SUB_ADMIN, reports_to=sa, login_id="m1", full_name="Meera Manager")
    m2 = make_user(Role.SUB_ADMIN, reports_to=sa, login_id="m2", full_name="Mohan Manager")
    e1 = make_user(Role.EMPLOYEE, reports_to=m1, login_id="e1", full_name="Esha One")
    e2 = make_user(Role.EMPLOYEE, reports_to=m1, login_id="e2", full_name="Eli Two")
    e3 = make_user(Role.EMPLOYEE, reports_to=m2, login_id="e3", full_name="Ezra Three")
    return {"sa": sa, "m1": m1, "m2": m2, "e1": e1, "e2": e2, "e3": e3}


def give(by, to, title="Work", status=S.ASSIGNED, due_in=48, at=None, **kw):
    t = make_task(by, to, title=title, due_at=now() + datetime.timedelta(hours=due_in), **kw)
    a = t.assignments.get()
    updates = {"status": status}
    if at is not None:
        updates["assigned_at"] = at
    TaskAssignment.objects.filter(pk=a.pk).update(**updates)
    a.refresh_from_db()
    return a


# --- the numbers ----------------------------------------------------------------------------------------------


def seed(o):
    base = now() - 10 * HOUR
    rows = [
        give(o["m1"], o["e1"], status=S.ASSIGNED, at=base),
        give(o["m1"], o["e1"], status=S.IN_PROGRESS, at=base + HOUR),
        give(o["m1"], o["e1"], status=S.SUBMITTED, at=base + 2 * HOUR, due_in=-5),  # submitted, never overdue
        give(o["m1"], o["e1"], status=S.APPROVED, at=base + 3 * HOUR, due_in=-5),  # done late: never overdue
        give(o["m1"], o["e1"], status=S.BLOCKED, at=base + 4 * HOUR, due_in=-1),  # open and late: overdue
        give(o["m1"], o["e1"], status=S.REMOVED, at=base + 5 * HOUR),  # not counted
        give(o["m1"], o["e1"], status=S.CANCELLED, at=base + 6 * HOUR),  # not counted
        give(o["m2"], o["e1"], status=S.ASSIGNED, at=base + HOUR),  # a different manager's work for e1
        give(o["m1"], o["e2"], status=S.CHANGES_REQUESTED, at=base + 2 * HOUR, due_in=-2),
    ]
    ct = make_task(o["m1"], o["e1"], title="A cancelled task")
    ct.is_cancelled = True
    ct.save(update_fields=["is_cancelled"])  # its assignment must not count either
    return rows


def expected(rows, viewer_assigned_by=None, user=None):
    """Independent recomputation of the card numbers in plain Python."""
    out = {}
    at = now()
    for a in rows:
        if a.status in (S.REMOVED, S.CANCELLED):
            continue
        key = a.user_id if viewer_assigned_by else a.assigned_by_id
        c = out.setdefault(key, {"total": 0, "active": 0, "completed": 0, "overdue": 0})
        c["total"] += 1
        c["active"] += a.status in (
            S.ASSIGNED,
            S.IN_PROGRESS,
            S.PAUSED,
            S.BLOCKED,
            S.CHANGES_REQUESTED,
            S.SUBMITTED,
        )
        c["completed"] += a.status == S.APPROVED
        c["overdue"] += a.status in (
            S.ASSIGNED,
            S.IN_PROGRESS,
            S.PAUSED,
            S.BLOCKED,
            S.CHANGES_REQUESTED,
        ) and (a.task.due_at < at)
    return out


def numbers(cs):
    return {c.user.pk: {k: getattr(c, k) for k in ("total", "active", "completed", "overdue")} for c in cs}


def test_a_managers_cards_match_an_independent_count(office):
    rows = [a for a in seed(office) if a.assigned_by_id == office["m1"].pk]
    got = numbers(cards.assigned_by_me_groups(office["m1"]))
    assert got == expected(rows, viewer_assigned_by=True)
    assert got[office["e1"].pk] == {"total": 5, "active": 4, "completed": 1, "overdue": 1}


def test_an_employees_cards_group_by_who_gave_the_work(office):
    rows = [a for a in seed(office) if a.user_id == office["e1"].pk]
    got = numbers(cards.assigned_to_me_groups(office["e1"]))
    assert got == expected(rows)
    assert set(got) == {office["m1"].pk, office["m2"].pk}


def test_submitted_and_finished_work_is_never_overdue_and_cancelled_things_do_not_count(office):
    seed(office)
    c = {x.user.pk: x for x in cards.assigned_by_me_groups(office["m1"])}[office["e1"].pk]
    assert c.overdue == 1  # only the blocked, late one
    assert c.total == 5  # not the removed, cancelled, or cancelled-task rows


def test_cards_are_ordered_by_who_was_given_work_first(office):
    base = now() - 5 * HOUR
    give(office["m1"], office["e2"], at=base + HOUR)
    give(office["m1"], office["e1"], at=base)
    assert [c.user.pk for c in cards.assigned_by_me_groups(office["m1"])] == [
        office["e1"].pk,
        office["e2"].pk,
    ]


def test_the_review_queue_holds_only_work_the_viewer_may_approve(office):
    mine = give(office["m1"], office["e1"], status=S.SUBMITTED)  # m1 created it
    reports = give(office["sa"], office["e2"], status=S.SUBMITTED)  # created by the boss, e2 reports to m1
    other = give(office["m2"], office["e3"], status=S.SUBMITTED)
    own = give(office["sa"], office["m1"], status=S.SUBMITTED)  # m1's own submission: never their review
    assert {a.pk for a in cards.review_queue(office["m1"])} == {mine.pk, reports.pk}
    assert {a.pk for a in cards.review_queue(office["sa"])} == {mine.pk, reports.pk, other.pk, own.pk}
    assert cards.review_queue(office["e1"]) == []


def test_team_view_is_wider_than_assigned_by_me_and_employees_get_none(office):
    give(office["sa"], office["e1"])  # the boss gave e1 work; m1 did not
    assert office["e1"].pk not in numbers(cards.assigned_by_me_groups(office["m1"]))
    assert office["e1"].pk in numbers(cards.team_groups(office["m1"]))  # but it is in their team's work
    assert cards.team_groups(office["e1"]) == []
    assert office["e3"].pk not in numbers(
        cards.team_groups(office["m1"])
    )  # another manager's people stay out


# --- order and paging -----------------------------------------------------------------------------------------


def test_oldest_assignment_first_and_pages_do_not_skip_or_repeat_even_with_equal_times(office):
    same = now() - 3 * HOUR
    made = [
        give(office["m1"], office["e1"], title=f"T{i}", at=same if i % 3 else same + HOUR) for i in range(60)
    ]
    seen, after = [], ""
    for _ in range(10):
        page = cards.group_page(office["m1"], "to", office["e1"], chip="all", after=after)
        seen += [a.pk for a in page.rows]
        after = page.more_after
        if not after:
            break
    assert len(seen) == 60 and len(set(seen)) == 60
    keys = {a.pk: (a.assigned_at, a.pk) for a in made}
    assert [keys[p] for p in seen] == sorted(keys.values())  # strictly (assigned_at, id)


def test_default_filter_is_open_and_chips_filter(office):
    for status in (
        S.ASSIGNED,
        S.IN_PROGRESS,
        S.BLOCKED,
        S.PAUSED,
        S.CHANGES_REQUESTED,
        S.SUBMITTED,
        S.APPROVED,
    ):
        give(office["m1"], office["e1"], status=status)
    give(office["m1"], office["e1"], status=S.IN_PROGRESS, due_in=-3)
    page = lambda chip: cards.group_page(office["m1"], "to", office["e1"], chip=chip)  # noqa: E731
    assert page("open").total == 7 and page("all").total == 8
    assert page("pending").total == 1 and page("progress").total == 2 and page("waiting").total == 2
    assert page("changes").total == 1 and page("submitted").total == 1 and page("completed").total == 1
    assert page("overdue").total == 1
    assert page("nonsense").filter == "open"  # unknown chip falls back to the default


def test_in_group_search_finds_by_title_or_code_only_inside_the_group(office):
    give(office["m1"], office["e1"], title="Quarterly GST filing")
    give(office["m1"], office["e1"], title="Payroll run")
    give(office["m2"], office["e1"], title="GST audit for the other manager")
    page = cards.group_page(office["m1"], "to", office["e1"], chip="all", q="gst")
    assert [a.task.title for a in page.rows] == ["Quarterly GST filing"]


# --- accents ---------------------------------------------------------------------------------------------------


def test_accent_order_overdue_then_soon_then_priority_and_done_is_muted(office):
    at = now()
    for status, due, prio, want in (
        (S.IN_PROGRESS, -2, Priority.LOW, "overdue"),
        (S.ASSIGNED, 3, Priority.LOW, "soon"),
        (S.ASSIGNED, 300, Priority.URGENT, "urgent"),
        (S.ASSIGNED, 300, Priority.HIGH, "high"),
        (S.ASSIGNED, 300, Priority.NORMAL, "normal"),
        (S.ASSIGNED, 300, Priority.LOW, "low"),
        (S.APPROVED, -50, Priority.URGENT, "done"),
        (S.SUBMITTED, -50, Priority.URGENT, "urgent"),
    ):
        a = give(office["m1"], office["e1"], status=status, due_in=due, priority=prio)
        assert cards.accent(a, at) == want, (status, due, prio)


# --- the pages --------------------------------------------------------------------------------------------------


def test_the_landing_shows_the_right_sections_for_each_role(office):
    give(office["m1"], office["e1"], title="Esha work")
    give(office["m1"], office["e1"], title="Waiting review", status=S.SUBMITTED)
    boss = login("boss").get("/app/tasks").content.decode()
    assert (
        "Assigned to me" in boss and "Needs my review" in boss and "Nobody has given you a task yet." in boss
    )
    m1 = login("m1").get("/app/tasks").content.decode()
    assert "Assigned by me" in m1 and "Esha One" in m1 and "Needs my review" in m1
    e1 = login("e1").get("/app/tasks").content.decode()
    assert "Meera Manager" in e1 and "Assigned by me" not in e1 and "Needs my review" not in e1
    assert 'href="/app/tasks/by/' in e1 and 'href="/app/tasks/to/' in m1


def test_empty_states(office):
    page = login("e3").get("/app/tasks").content.decode()
    assert "Nobody has given you a task yet." in page


def test_group_pages_open_for_people_you_work_with_and_404_for_anyone_else(office):
    give(office["m1"], office["e1"])
    assert login("e1").get(f"/app/tasks/by/{office['m1'].pk}").status_code == 200
    assert login("m1").get(f"/app/tasks/to/{office['e1'].pk}").status_code == 200
    for who, url in (
        ("e2", f"/app/tasks/by/{office['m1'].pk}"),  # e2 holds nothing from m1
        ("m2", f"/app/tasks/to/{office['e1'].pk}"),  # m2 never gave e1 work in this fixture
        ("e1", f"/app/tasks/by/{uuid.uuid4()}"),
        ("e1", "/app/tasks/by/not-a-uuid"),
        ("e1", f"/app/tasks/to/{office['m1'].pk}"),  # an employee has assigned nobody
    ):
        assert login(who).get(url).status_code == 404, (who, url)


def test_cards_open_the_task_and_carry_a_safe_way_back(office):
    a = give(office["m1"], office["e1"], title="Reconcile ledger")
    page = login("m1").get(f"/app/tasks/to/{office['e1'].pk}").content.decode()
    assert f"/app/tasks/{a.task_id}?from=" in page and "Reconcile ledger" in page
    detail = (
        login("m1")
        .get(f"/app/tasks/{a.task_id}?from=/app/tasks/to/{office['e1'].pk}%3Ffilter%3Dall")
        .content.decode()
    )
    assert f'href="/app/tasks/to/{office["e1"].pk}?filter=all"' in detail and "Back" in detail


@pytest.mark.parametrize(
    "bad",
    ["https://evil.example/app/tasks", "//evil.example", "/app/tasksx/../../etc", "/login", "javascript:alert(1)",
     "/app/tasks\\evil", "/app/tasks\n/x", "", "/app/tasks://x", "/app/dashboard"],
)  # fmt: skip
def test_the_back_link_refuses_anything_that_could_leave_the_task_pages(office, bad):
    a = give(office["m1"], office["e1"])
    from urllib.parse import quote

    detail = login("m1").get(f"/app/tasks/{a.task_id}?from={quote(bad, safe='')}").content.decode()
    assert "&larr; Back" not in detail
    assert "evil" not in detail


def test_safe_back_accepts_task_paths_with_queries():
    from ots.tasks.views import safe_back

    assert safe_back("/app/tasks") == "/app/tasks"
    assert safe_back("/app/tasks/by/abc?filter=all&q=x") == "/app/tasks/by/abc?filter=all&q=x"


def test_the_flat_table_and_dashboard_links_moved_to_all(office):
    give(office["m1"], office["e1"], title="In the table")
    assert "In the table" in login("m1").get("/app/tasks/all").content.decode()
    dash = login("m1").get("/app/dashboard").content.decode()
    assert 'href="/app/tasks/all?status=' in dash and 'href="/app/tasks?status=' not in dash


def test_landing_and_group_page_query_counts_do_not_grow_with_people_or_tasks(office):
    def build(n_people, n_tasks):
        for i in range(n_people):
            e = make_user(Role.EMPLOYEE, reports_to=office["m1"], login_id=f"x{n_people}_{i}")
            for j in range(n_tasks):
                give(office["m1"], e, title=f"T{i}-{j}")

    c = login("m1")
    build(2, 2)
    with CaptureQueriesContext(connection) as small:
        assert c.get("/app/tasks").status_code == 200
        assert c.get(f"/app/tasks/to/{office['e1'].pk}?filter=all").status_code in (200, 404)
    build(12, 5)
    with CaptureQueriesContext(connection) as big:
        assert c.get("/app/tasks").status_code == 200
        assert c.get(f"/app/tasks/to/{office['e1'].pk}?filter=all").status_code in (200, 404)
    assert len(big) == len(small), (len(small), len(big))
