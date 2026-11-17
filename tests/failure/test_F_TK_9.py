"""F-TK-9: card grouping never exposes another manager's assignments, nor counts or names of people outside
the viewer's scope (page, group route, query-string tampering, guessed ids)."""

import datetime
import re
import uuid

import pytest
from django.test import Client
from ots.accounts.models import Role
from ots.core.clock import now
from ots.tasks import cards

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
    a = make_user(Role.SUB_ADMIN, reports_to=sa, login_id="ma", full_name="Alpha Manager")
    b = make_user(Role.SUB_ADMIN, reports_to=sa, login_id="mb", full_name="Bravo Manager")
    ea = make_user(Role.EMPLOYEE, reports_to=a, login_id="ea", full_name="Ann Employee")
    eb = make_user(Role.EMPLOYEE, reports_to=b, login_id="eb", full_name="Ben Employee")
    mine = make_task(a, ea, title="Alpha's own filing")
    secret = make_task(b, eb, title="Bravo's SECRET merger")
    return {"sa": sa, "a": a, "b": b, "ea": ea, "eb": eb, "mine": mine, "secret": secret}


def test_an_employees_page_never_names_another_managers_people_or_tasks(world):
    page = login("ea").get("/app/tasks").content.decode()
    assert "Alpha Manager" in page
    for leak in ("Bravo", "Ben Employee", "SECRET"):
        assert leak not in page, leak


def test_a_manager_sees_only_their_own_assignments_and_people_on_the_landing(world):
    page = login("ma").get("/app/tasks").content.decode()
    assert "Ann Employee" in page and "Ben Employee" not in page and "SECRET" not in page
    assert [c.user.pk for c in cards.assigned_by_me_groups(world["a"])] == [world["ea"].pk]
    team = login("ma").get("/app/tasks?team=1").content.decode()
    assert "Ben Employee" not in team and "SECRET" not in team


def test_a_group_route_with_somebody_elses_id_is_a_plain_404(world):
    for who, url in (
        ("ea", f"/app/tasks/by/{world['b'].pk}"),
        ("ea", f"/app/tasks/to/{world['eb'].pk}"),
        ("ma", f"/app/tasks/to/{world['eb'].pk}"),
        ("ma", f"/app/tasks/to/{world['eb'].pk}?team=1"),
        ("mb", f"/app/tasks/to/{world['ea'].pk}"),
        ("ea", f"/app/tasks/by/{uuid.uuid4()}"),
    ):
        r = login(who).get(url)
        assert r.status_code == 404, (who, url)
        assert "SECRET" not in r.content.decode() and "Bravo" not in r.content.decode()


def test_the_404_is_the_same_for_a_missing_person_and_for_a_person_you_have_nothing_with(world):
    c = login("ea")
    real = c.get(f"/app/tasks/by/{world['eb'].pk}")
    ghost = c.get(f"/app/tasks/by/{uuid.uuid4()}")
    assert real.status_code == ghost.status_code == 404
    text = lambda r: re.sub(r"[0-9a-f-]{36}|req[-_ ]?\w+", "ID", re.sub(r"\s+", " ", r.content.decode()))  # noqa: E731
    assert text(real) == text(ghost)  # nothing in the answer tells "no such person" from "not your person"


def test_tampering_with_filters_paging_or_team_cannot_widen_a_group(world):
    c = login("ea")
    for q in (
        "?filter=all", "?filter=overdue", "?filter=../../x", "?q=SECRET", "?team=1", "?after=garbage",
        "?after=2000-01-01T00:00:00%2B00:00|" + str(uuid.uuid4()), "?filter=all&q=%25&team=1",
    ):  # fmt: skip
        r = c.get(f"/app/tasks/by/{world['a'].pk}{q}")
        assert r.status_code == 200, q
        assert "Bravo's SECRET merger" not in r.content.decode().replace("&#x27;", "'"), q
    assert c.get(f"/app/tasks/to/{world['a'].pk}?team=1").status_code == 404  # "to" for an employee: nothing


def test_an_employee_cannot_use_the_team_view_to_widen_anything(world):
    assert cards.team_groups(world["ea"]) == []
    assert "Ben Employee" not in login("ea").get("/app/tasks?team=1").content.decode()


def test_the_review_strip_holds_only_what_the_viewer_may_approve(world):
    from ots.tasks.models import AssignmentStatus, TaskAssignment

    TaskAssignment.objects.filter(task=world["secret"]).update(status=AssignmentStatus.SUBMITTED)
    assert "SECRET" not in login("ma").get("/app/tasks").content.decode()  # not Alpha's to review
    assert "SECRET" in login("mb").get("/app/tasks").content.decode()  # Bravo created it
    assert "SECRET" in login("boss").get("/app/tasks").content.decode()


def test_counts_on_a_card_never_include_work_from_another_manager(world):
    other = make_task(
        world["b"], world["ea"], title="Bravo gives Ann work too", due_at=now() + datetime.timedelta(1)
    )
    by_alpha = {c.user.pk: c for c in cards.assigned_to_me_groups(world["ea"])}
    assert by_alpha[world["a"].pk].total == 1 and by_alpha[world["b"].pk].total == 1
    assert other.assignments.count() == 1
    page = login("ea").get(f"/app/tasks/by/{world['a'].pk}?filter=all").content.decode()
    assert "Alpha&#x27;s own filing" in page or "Alpha's own filing" in page
    assert "Bravo gives Ann work too" not in page
