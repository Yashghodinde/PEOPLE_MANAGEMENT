"""IMP-15.7: whole-card colours from the assignment's state (LAND-05..12) and the remembered Cards/Table choice
(LAND-01..04)."""

import pytest
from ots.tasks import cards
from ots.tasks.models import AssignmentStatus as S
from ots.tasks.models import Priority, Task

from tests.tasks.test_cards import give, login, office  # noqa: F401  (the fixture and helpers)

pytestmark = pytest.mark.django_db

EXPECTED = {
    S.ASSIGNED: "grey", S.IN_PROGRESS: "yellow", S.PAUSED: "yellow", S.BLOCKED: "red",
    S.CHANGES_REQUESTED: "pink", S.SUBMITTED: "blue", S.APPROVED: "green",
}  # fmt: skip


@pytest.mark.parametrize("status,tone", list(EXPECTED.items()))
def test_each_state_has_its_colour(office, status, tone):  # noqa: F811
    a = give(office["m1"], office["e1"], status=status)
    assert cards.card_tone(a) == tone


def test_a_cancelled_task_is_muted_whatever_the_assignment_said(office):  # noqa: F811
    a = give(office["m1"], office["e1"], status=S.IN_PROGRESS)
    Task.objects.filter(pk=a.task_id).update(is_cancelled=True)
    a.task.refresh_from_db()
    assert cards.card_tone(a) == "muted"


def test_priority_and_due_date_never_change_the_colour(office):  # noqa: F811
    urgent_late = give(office["m1"], office["e1"], status=S.IN_PROGRESS, due_in=-30, priority=Priority.URGENT)
    low_far = give(office["m1"], office["e1"], status=S.IN_PROGRESS, due_in=500, priority=Priority.LOW)
    assert cards.card_tone(urgent_late) == cards.card_tone(low_far) == "yellow"
    done_late = give(office["m1"], office["e1"], status=S.APPROVED, due_in=-100)
    assert cards.card_tone(done_late) == "green"  # an old due date does not make finished work look overdue


def test_the_card_is_coloured_all_over_and_has_no_side_stripe(office):  # noqa: F811
    give(office["m1"], office["e1"], status=S.SUBMITTED)
    page = login("m1").get("/app/tasks/to/" + str(office["e1"].pk)).content.decode()
    assert "task-tone-blue" in page and 'data-tone="blue"' in page
    assert "border-l-4" not in page and "border-l-bad" not in page and "border-l-accent" not in page


def test_the_switch_is_on_both_pages_and_the_choice_is_remembered(office):  # noqa: F811
    c = login("e1")
    landing = c.get("/app/tasks?view=cards").content.decode()
    assert "Card view" in landing and "Table view" in landing
    r = c.get("/app/tasks/all?view=table")
    assert r.status_code == 200 and b'aria-label="Tasks view"' in r.content
    office["e1"].refresh_from_db()
    assert office["e1"].task_view == "table"
    back = c.get("/app/tasks")  # opening Tasks again lands on what they chose last
    assert back.status_code == 302 and back["Location"] == "/app/tasks/all"
    c.get("/app/tasks?view=cards")
    office["e1"].refresh_from_db()
    assert office["e1"].task_view == "cards"
    assert c.get("/app/tasks").status_code == 200


def test_the_choice_belongs_to_the_person_and_a_bad_value_is_ignored(office):  # noqa: F811
    login("e1").get("/app/tasks/all?view=table")
    assert login("e2").get("/app/tasks").status_code == 200  # someone else is not moved to the table
    login("e2").get("/app/tasks?view=banana")
    office["e2"].refresh_from_db()
    assert office["e2"].task_view == "cards"


def test_filters_survive_the_redirect_and_the_form_posts_to_the_table(office):  # noqa: F811
    c = login("e1")
    c.get("/app/tasks/all?view=table")
    r = c.get("/app/tasks?status=IN_PROGRESS")
    assert r.status_code == 302 and r["Location"] == "/app/tasks/all?status=IN_PROGRESS"
    assert 'action="/app/tasks/all"' in c.get("/app/tasks/all").content.decode()
