"""M07.1: task codes, search columns, the one-live-assignment rule, append-only history, the overdue selector."""

import datetime
import threading

import pytest
from django.db import DatabaseError, IntegrityError, connection, transaction
from freezegun import freeze_time
from ots.accounts.models import Company, Role
from ots.tasks.codes import next_task_code
from ots.tasks.models import AssignmentStatus, Task, TaskAssignment, TaskComment, TaskEvent

from tests.accounts.factories import make_user
from tests.tasks.factories import make_task

pytestmark = pytest.mark.django_db
S = AssignmentStatus


@pytest.fixture
def people():
    sa = make_user(Role.SUPER_ADMIN)
    sub = make_user(Role.SUB_ADMIN, reports_to=sa)
    emp = make_user(Role.EMPLOYEE, reports_to=sub)
    return sa, sub, emp


# --- task codes -----------------------------------------------------------------------------------------------


def test_codes_count_up_within_a_year_and_start_over_in_the_next():
    with freeze_time("2026-12-31 10:00:00"):
        a, b = next_task_code(), next_task_code()
    with freeze_time("2027-01-01 10:00:00"):
        c = next_task_code()
    assert a.startswith("T-2026-") and b.startswith("T-2026-") and int(b[-6:]) == int(a[-6:]) + 1
    assert c == "T-2027-000001"  # the new year starts again at 1


def test_a_year_without_a_sequence_gets_one_on_demand():
    assert next_task_code(2099) == "T-2099-000001" and next_task_code(2099) == "T-2099-000002"
    with connection.cursor() as cur:
        cur.execute("SELECT to_regclass('task_code_seq_2099') IS NOT NULL")
        assert cur.fetchone()[0]


def test_the_year_cannot_inject_sql():
    with pytest.raises((ValueError, TypeError)):
        next_task_code("2026; DROP TABLE tasks")  # type: ignore[arg-type]


@pytest.fixture
def keep_default_company():
    yield
    Company.objects.get_or_create(code="DEFAULT", defaults={"name": "Default company"})


@pytest.mark.django_db(transaction=True)
def test_concurrent_creates_never_share_a_code(keep_default_company):
    codes, errors = [], []

    def worker():
        try:
            codes.extend(next_task_code(2098) for _ in range(25))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors and len(codes) == 100 and len(set(codes)) == 100


# --- search columns ----------------------------------------------------------------------------------------


def test_the_search_vector_and_trigram_index_find_tasks(people):
    sa, _, emp = people
    make_task(sa, emp, title="Reconcile the vendor invoices", description="October ledger")
    make_task(sa, emp, title="Plan the diwali schedule", description="Shift roster")
    with connection.cursor() as cur:
        cur.execute("SELECT title FROM tasks WHERE search_vector @@ plainto_tsquery('simple', 'ledger')")
        assert [r[0] for r in cur.fetchall()] == ["Reconcile the vendor invoices"]
        cur.execute(
            "SELECT title FROM tasks WHERE search_vector @@ plainto_tsquery('simple', %s)",
            [Task.objects.first().task_code],
        )
        assert len(cur.fetchall()) == 1  # the code itself is searchable
        cur.execute(
            "SELECT title FROM tasks WHERE title ILIKE '%voic%' ORDER BY title"
        )  # the middle of a word
        assert [r[0] for r in cur.fetchall()] == ["Reconcile the vendor invoices"]
        cur.execute("SET LOCAL enable_seqscan = off")
        cur.execute("EXPLAIN SELECT 1 FROM tasks WHERE title ILIKE '%voic%'")
        assert "tasks_title_trgm_idx" in " ".join(r[0] for r in cur.fetchall())  # and the index serves it
        cur.execute(
            "SELECT indexdef FROM pg_indexes WHERE indexname IN ('tasks_search_idx','tasks_title_trgm_idx')"
        )
        assert {("gin" in r[0].lower()) for r in cur.fetchall()} == {True}


# --- assignments ------------------------------------------------------------------------------------------------


def test_a_person_has_one_live_assignment_per_task_but_can_be_added_again_after_removal(people):
    sa, _, emp = people
    task = make_task(sa, emp)
    with pytest.raises(IntegrityError), transaction.atomic():
        TaskAssignment.objects.create(task=task, user=emp, assigned_by=sa)
    TaskAssignment.objects.filter(task=task, user=emp).update(
        status=S.REMOVED, removed_at=datetime.datetime.now(datetime.UTC)
    )
    TaskAssignment.objects.create(task=task, user=emp, assigned_by=sa)  # fine: the old one is removed
    assert TaskAssignment.objects.filter(task=task, user=emp).count() == 2


def test_nothing_that_matters_can_be_deleted(people):
    sa, _, emp = people
    task = make_task(sa, emp)
    from django.db.models import ProtectedError

    for obj in (task, emp, sa):
        with pytest.raises((ProtectedError, DatabaseError)), transaction.atomic():
            obj.delete()


# --- append-only history ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("table,make", [("task_events", "event"), ("task_comments", "comment")])
def test_history_and_comments_cannot_be_changed_or_deleted(people, table, make):
    sa, _, emp = people
    task = make_task(sa, emp)
    row = (
        TaskEvent.objects.create(task=task, actor=sa, event_type="task.created")
        if make == "event"
        else TaskComment.objects.create(task=task, author=sa, body="hello")
    )
    column = "event_type" if make == "event" else "body"
    with connection.cursor() as cur:
        cur.execute("SET ROLE ots_app")
        try:
            statements = (
                f"UPDATE {table} SET {column}='x' WHERE id=%s",  # noqa: S608 (names come from the parameters)
                f"DELETE FROM {table} WHERE id=%s",  # noqa: S608
            )
            for sql in statements:
                with pytest.raises(DatabaseError), transaction.atomic():
                    cur.execute(sql, [row.pk])
            cur.execute("SELECT has_table_privilege('ots_app', %s, 'UPDATE')", [table])
            assert cur.fetchone()[0] is False
        finally:
            cur.execute("RESET ROLE")
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cur:
        cur.execute(f"UPDATE {table} SET {column}='x' WHERE id=%s", [row.pk])  # noqa: S608  (the owner too)


# --- overdue ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,overdue",
    [(S.ASSIGNED, True), (S.IN_PROGRESS, True), (S.PAUSED, True), (S.BLOCKED, True), (S.CHANGES_REQUESTED, True),
     (S.SUBMITTED, False), (S.APPROVED, False), (S.REMOVED, False), (S.CANCELLED, False)],
)  # fmt: skip
def test_overdue_follows_the_open_statuses(people, status, overdue):
    sa, _, emp = people
    task = make_task(sa, emp, due_in_hours=-1)  # due an hour ago
    TaskAssignment.objects.filter(task=task).update(status=status)
    assert Task.objects.overdue().filter(pk=task.pk).exists() is overdue


def test_overdue_needs_a_past_due_time_and_a_live_task(people):
    sa, _, emp = people
    future = make_task(sa, emp, due_in_hours=3)
    cancelled = make_task(sa, emp, due_in_hours=-3, is_cancelled=True)
    late = make_task(sa, emp, due_in_hours=-3)
    assert set(Task.objects.overdue().values_list("pk", flat=True)) == {late.pk}
    with freeze_time(datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=5)):
        assert set(Task.objects.overdue().values_list("pk", flat=True)) == {late.pk, future.pk}
    assert cancelled.pk not in set(Task.objects.overdue().values_list("pk", flat=True))
