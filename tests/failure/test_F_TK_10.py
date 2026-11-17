"""F-TK-10: unread indicators on one person's card never come from another person's conversation or tasks, and
are per viewer."""

import pytest
from django.test import Client
from ots.accounts.models import Role
from ots.notifications import services as note_services
from ots.tasks import cards, services

from tests.accounts.factories import make_user
from tests.tasks.factories import make_task

pytestmark = pytest.mark.django_db
PW = "correct horse battery"


def login(login_id):
    c = Client()
    assert c.post("/login", {"login_id": login_id, "password": PW}).status_code == 302
    return c


@pytest.fixture
def world():
    sa = make_user(Role.SUPER_ADMIN, login_id="boss", full_name="Boss Person")
    m = make_user(Role.SUB_ADMIN, reports_to=sa, login_id="mgr", full_name="Meera Manager")
    n = make_user(Role.SUB_ADMIN, reports_to=sa, login_id="mgr2", full_name="Nikhil Manager")
    e1 = make_user(Role.EMPLOYEE, reports_to=m, login_id="e1", full_name="Esha One")
    e2 = make_user(Role.EMPLOYEE, reports_to=m, login_id="e2", full_name="Eli Two")
    t1 = make_task(m, e1, title="Esha's task")
    t2 = make_task(m, e2, title="Eli's task")
    shared = make_task(m, e1, e2, title="Shared task")
    other = make_task(n, e1, title="Nikhil gives Esha work")
    return {
        "sa": sa,
        "m": m,
        "n": n,
        "e1": e1,
        "e2": e2,
        "t1": t1,
        "t2": t2,
        "shared": shared,
        "other": other,
    }


def unread(viewer, fn):
    return {k: v["text"] for k, v in fn(viewer).items()}


def test_two_people_write_at_once_and_each_card_shows_only_its_own(world):
    services.add_comment(world["e1"], world["t1"].pk, "From Esha")
    services.add_comment(world["e2"], world["t2"].pk, "From Eli")
    services.add_comment(world["e2"], world["t2"].pk, "From Eli again")
    got = unread(world["m"], cards.unread_by_assignee)
    assert got == {world["e1"].pk: 1, world["e2"].pk: 2}
    page = login("mgr").get("/app/tasks").content.decode()
    assert "Esha One: 2 active, 0 completed, 0 overdue, 2 in all, 1 unread message" in page
    assert "Eli Two: 2 active, 0 completed, 0 overdue, 2 in all, 2 unread messages" in page


def test_a_message_on_the_managers_task_lands_on_the_assigners_card_for_the_employee(world):
    services.add_comment(world["m"], world["t1"].pk, "Please hurry")
    assert unread(world["e1"], cards.unread_by_assigner) == {world["m"].pk: 1}
    assert unread(world["e2"], cards.unread_by_assigner) == {}  # e2 is not on that task
    assert unread(world["m"], cards.unread_by_assigner) == {}  # the author reads nothing from themself


def test_the_other_managers_conversation_stays_on_the_other_managers_card(world):
    services.add_comment(world["n"], world["other"].pk, "Nikhil here")
    assert unread(world["e1"], cards.unread_by_assigner) == {world["n"].pk: 1}
    assert world["m"].pk not in unread(world["e1"], cards.unread_by_assigner)
    assert unread(world["m"], cards.unread_by_assignee) == {}  # Meera sees none of Nikhil's work


def test_a_shared_task_counts_on_each_authors_own_card(world):
    services.add_comment(world["e1"], world["shared"].pk, "Esha on the shared task")
    services.add_comment(world["e2"], world["shared"].pk, "Eli on the shared task")
    assert unread(world["m"], cards.unread_by_assignee) == {world["e1"].pk: 1, world["e2"].pk: 1}


def test_reading_one_task_clears_only_that_task_and_only_for_that_person(world):
    services.add_comment(world["m"], world["t1"].pk, "One")
    services.add_comment(world["m"], world["shared"].pk, "Two")
    services.add_comment(world["e2"], world["shared"].pk, "Three")
    note_services.mark_task_read(world["e1"], world["t1"].pk)
    assert unread(world["e1"], cards.unread_by_assigner) == {
        world["m"].pk: 2
    }  # "Two" and "Three" are on the shared task
    assert cards.unread_by_task(world["e2"], [world["shared"].pk, world["t1"].pk]) == {
        world["shared"].pk: {"text": 1, "voice": 0}
    }  # e2's own read state is untouched
    note_services.mark_task_read(world["e1"], world["shared"].pk)
    assert unread(world["e1"], cards.unread_by_assigner) == {}


def test_a_message_in_a_task_the_viewer_cannot_see_shows_nowhere(world):
    services.add_comment(world["e2"], world["t2"].pk, "Private to Eli's task")
    assert unread(world["e1"], cards.unread_by_assigner) == {}
    assert unread(world["e1"], cards.unread_by_assignee) == {}
    assert unread(world["n"], cards.unread_by_assignee) == {}
    assert cards.unread_by_task(world["e1"], [world["t2"].pk]) == {}
    page = login("e1").get("/app/tasks").content.decode()
    assert "unread message" not in page


def test_unread_state_is_per_viewer_not_global(world):
    services.add_comment(world["e1"], world["shared"].pk, "Hello team")
    assert unread(world["m"], cards.unread_by_assignee) == {world["e1"].pk: 1}
    assert unread(world["e2"], cards.unread_by_assigner) == {world["m"].pk: 1}
    note_services.mark_task_read(world["m"], world["shared"].pk)
    assert unread(world["m"], cards.unread_by_assignee) == {}
    assert unread(world["e2"], cards.unread_by_assigner) == {world["m"].pk: 1}  # e2 still has it unread


def test_the_task_card_carries_its_own_unread_count(world):
    services.add_comment(world["m"], world["t1"].pk, "Check this")
    page = login("e1").get(f"/app/tasks/by/{world['m'].pk}?filter=all").content.decode()
    assert "1 unread message" in page
    login("e1").get(f"/app/tasks/{world['t1'].pk}")  # opening the task clears it
    again = login("e1").get(f"/app/tasks/by/{world['m'].pk}?filter=all").content.decode()
    assert "unread message" not in again
