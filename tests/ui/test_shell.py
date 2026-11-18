"""IMP-15.2: the profile menu names the signed-in person; a lunch logout is its own session end; the shell has
the ids the page swap relies on."""

import pytest
from ots.attendance.models import SessionEndReason, WorkSession

from tests.files.conftest import login

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("dirs")]


def test_the_menu_starts_with_the_name_and_role_then_the_options(office):
    page = login("emp").get("/app/dashboard").content.decode()
    menu = page.split('aria-label="Signed in as"', 1)[1]
    assert office["emp"].full_name in menu.split("Profile", 1)[0]
    for text in ("Profile", "Log out for the lunch break", "Log out for the day", "Log out"):
        assert text in menu, text
    assert menu.index("Profile") < menu.index("lunch break") < menu.index("for the day")


def test_the_shell_exposes_the_regions_a_page_swap_replaces(office):
    page = login("emp").get("/app/dashboard").content.decode()
    for marker in ('id="main"', 'id="shell-title"', 'id="sidebar-nav"', 'id="shell-tools"'):
        assert marker in page, marker


def test_lunch_logout_ends_the_session_as_a_lunch_break_and_logs_out(office):
    c = login("emp")
    r = c.post("/logout", {"reason": "lunch"})
    assert r.status_code == 302 and r["Location"] == "/login"
    assert c.get("/app/dashboard").status_code == 302  # no longer signed in
    ended = WorkSession.objects.filter(user=office["emp"]).exclude(logout_at=None)
    assert all(s.logout_reason in ("", SessionEndReason.LUNCH) for s in ended)


def test_a_plain_logout_is_still_a_manual_break(office):
    c = login("emp")
    assert c.post("/logout").status_code == 302
    assert all(
        s.logout_reason in ("", SessionEndReason.MANUAL)
        for s in WorkSession.objects.filter(user=office["emp"]).exclude(logout_at=None)
    )
