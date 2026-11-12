"""M07.4: end-of-day closure is all or nothing, then logs the person out; 'Resume all' next morning."""

import uuid

import pytest
from django.test import Client
from ots.tasks import services
from ots.tasks.models import AssignmentStatus, PauseReason

from tests.tasks.factories import make_task

pytestmark = pytest.mark.django_db
PW = "correct horse battery"


def login(login_id):
    c = Client()
    assert c.post("/login", {"login_id": login_id, "password": PW}).status_code == 302
    return c


@pytest.fixture
def two_lines(org):
    first = make_task(org["mgr"], org["emp"], title="First job").assignments.get()
    second = make_task(org["mgr"], org["emp"], title="Second job").assignments.get()
    for a in (first, second):
        services.transition(a.pk, "start", org["emp"])
    return first, second


def status(a):
    a.refresh_from_db()
    return a.status


def test_every_choice_is_applied_together_then_the_person_is_logged_out(org, two_lines):
    first, second = two_lines
    c = login("emp")
    r = c.post(
        "/app/day-close",
        {
            f"choice_{first.pk}": "complete", f"note_{first.pk}": "Finished and filed.",
            f"choice_{second.pk}": "continue",
        },
    )  # fmt: skip
    assert r.status_code == 302 and r["Location"] == "/login"
    assert status(first) == AssignmentStatus.SUBMITTED
    assert status(second) == AssignmentStatus.PAUSED and second.pause_reason == PauseReason.END_OF_DAY
    assert c.get("/app/dashboard").status_code == 302  # logged out


def test_one_bad_choice_means_nothing_is_saved(org, two_lines):
    first, second = two_lines
    c = login("emp")
    r = c.post(
        "/app/day-close",
        {f"choice_{first.pk}": "continue", f"choice_{second.pk}": "complete", f"note_{second.pk}": "x"},
    )
    body = r.content.decode()
    assert r.status_code == 422 and "Nothing was saved" in body and "At least 10 characters" in body
    assert status(first) == AssignmentStatus.IN_PROGRESS and status(second) == AssignmentStatus.IN_PROGRESS
    assert c.get("/app/dashboard").status_code == 200  # still logged in


def test_a_line_left_without_a_choice_is_an_error(org, two_lines):
    first, _ = two_lines
    r = login("emp").post("/app/day-close", {f"choice_{first.pk}": "continue"})
    assert r.status_code == 422
    assert status(first) == AssignmentStatus.IN_PROGRESS


def test_waiting_needs_a_reason_and_a_note(org, two_lines):
    first, second = two_lines
    r = login("emp").post(
        "/app/day-close", {f"choice_{first.pk}": "waiting", f"choice_{second.pk}": "continue"}
    )
    assert r.status_code == 422 and status(first) == AssignmentStatus.IN_PROGRESS
    r = login("emp").post(
        "/app/day-close",
        {
            f"choice_{first.pk}": "waiting", f"block_reason_{first.pk}": "DEPENDENCY",
            f"block_note_{first.pk}": "Need the vendor list", f"choice_{second.pk}": "continue",
        },
    )  # fmt: skip
    assert r.status_code == 302 and status(first) == AssignmentStatus.BLOCKED


def test_blocked_work_keeps_its_wait_and_nothing_open_just_logs_out(org, two_lines):
    first, second = two_lines
    services.transition(
        first.pk, "block", org["emp"], block_reason="DEPENDENCY", block_note="Waiting for vendor"
    )
    r = login("emp").post("/app/day-close", {f"choice_{second.pk}": "continue"})
    assert r.status_code == 302 and status(first) == AssignmentStatus.BLOCKED
    assert login("peer").post("/app/day-close").status_code == 302  # nobody has open work: just logs out


def test_a_repeated_key_does_not_apply_twice(org, two_lines):
    first, second = two_lines
    c, key = login("emp"), {"HTTP_IDEMPOTENCY_KEY": str(uuid.uuid4())}
    data = {f"choice_{first.pk}": "continue", f"choice_{second.pk}": "continue"}
    assert c.post("/app/day-close", data, **key).status_code == 302
    assert Client().get("/login").status_code == 200
    again = login("emp").post("/app/day-close", data, **key)  # a retry of the same request
    assert again.status_code in (302, 409)
    assert status(first) == AssignmentStatus.PAUSED


def test_resume_all_brings_back_everything_paused(org, two_lines):
    first, second = two_lines
    login("emp").post("/app/day-close", {f"choice_{first.pk}": "continue", f"choice_{second.pk}": "continue"})
    c = login("emp")
    assert "Resume all" in c.get("/app/dashboard").content.decode()
    assert c.post("/app/day-resume").status_code == 302
    assert status(first) == AssignmentStatus.IN_PROGRESS and status(second) == AssignmentStatus.IN_PROGRESS
    assert "Resume all" not in c.get("/app/dashboard").content.decode()


def test_the_page_lists_only_my_open_work(org, two_lines):
    body = login("emp").get("/app/day-close").content.decode()
    assert "First job" in body and "Second job" in body
    assert "First job" not in login("peer").get("/app/day-close").content.decode()
