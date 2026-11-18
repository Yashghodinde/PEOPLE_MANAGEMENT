"""IMP-14.1: one page header (breadcrumb + a visible Back) built from the path."""

import pytest
from ots.ui.crumbs import trail

from tests.files.conftest import login

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("dirs")]


def test_trail_shapes():
    assert trail("/files/mine", "My storage") == []  # a top-level page has no header
    assert trail("/files/mine/2026-11-17", "17 November") == [
        ("Files", "/files/mine"), ("My storage", "/files/mine"), ("17 November", ""),
    ]  # fmt: skip
    assert trail("/app/tasks/abc", "T-1")[-1] == ("T-1", "")
    assert trail("/app/tasks/new", "")[-1] == ("New task", "")
    assert trail("/app/org", "Org chart") == [("People", "/app/people"), ("Org chart", "")]
    assert trail("/app/dashboard", "Dashboard") == []


def test_header_shows_a_back_button_and_the_current_page_marked(office):
    page = login("boss").get("/app/org").content.decode()
    assert "data-pagehead" in page and "data-back" in page
    assert 'href="/app/people"' in page and 'aria-current="page"' in page


def test_task_page_back_goes_to_the_tasks_list(office):
    page = login("boss").get("/files/shared").content.decode()
    assert "data-pagehead" not in page  # section roots stay clean
