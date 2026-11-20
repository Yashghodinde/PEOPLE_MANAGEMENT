"""IMP-06.1: every /files page shows the same Quick Search box and section strip, the right section lit; HR has none."""

import pytest
from ots.accounts.models import Role

from tests.accounts.factories import make_user
from tests.files.conftest import login

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("dirs")]

PAGES = {
    "/files/mine": "mine",
    "/files/tasks": "tasks",
    "/files/ids": "ids",
    "/files/library": "library",
    "/files/shared": "shared",
}
LABELS = ["My storage", "Task files", "File IDs", "Library", "Shared with me"]


@pytest.mark.parametrize("who", ["boss", "sub", "emp"])
def test_every_page_has_the_search_box_and_all_sections(office, who):
    c = login(who)
    for url in [*PAGES, "/files/search"]:
        page = c.get(url).content.decode()
        assert 'aria-label="Quick search"' in page, (who, url)
        for label in LABELS:
            assert f">{label}</a>" in page, (who, url, label)


def test_the_section_you_are_in_is_lit_and_only_that_one(office):
    c = login("emp")
    for url in PAGES:
        page = c.get(url).content.decode()
        strip = page[page.index('aria-label="Files sections"') :]
        strip = strip[: strip.index("</nav>")]
        assert strip.count('aria-current="page"') == 1, url
        i = strip.index('aria-current="page"')
        assert f'href="{url}"' in strip[max(0, i - 80) : i], url


def test_the_deleted_section_is_for_the_super_admin_only(office):
    assert ">Deleted</a>" in login("boss").get("/files/mine").content.decode()
    assert ">Deleted</a>" not in login("sub").get("/files/mine").content.decode()


def test_hr_has_only_my_storage_in_files_and_the_sidebar_points_at_it(office):
    make_user(Role.HR, reports_to=office["sa"], login_id="hr")
    hr = login("hr")
    page = hr.get("/files/mine")
    assert page.status_code == 200
    html = page.content.decode()
    for label in ("Task files", "File IDs", "Library", "Shared with me", "People's storage", "Deleted"):
        assert f">{label}</a>" not in html, label
    assert 'href="/files/mine"' in hr.get("/app/dashboard").content.decode()
    assert 'href="/files/mine"' in login("emp").get("/app/dashboard").content.decode()
