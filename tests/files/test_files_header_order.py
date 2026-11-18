"""IMP-16.2: on every Files page the order is Back/breadcrumb, then the search row, then the section tabs, then
the content, and the same tabs show wherever you are."""

import uuid

import pytest
from ots.files import folders, library
from ots.workspaces import services as ws_services

from tests.files.conftest import login
from tests.files.test_personal import mine_upload

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("dirs")]


@pytest.fixture
def seeded(office):
    v = mine_upload("emp", name="2026-11-17_Order-notes.txt")
    fol = folders.create_personal(office["emp"], "Order folder")
    lib = library.create_folder(office["sa"], "Order library")
    ws = ws_services.create_workspace(office["sub"], name="Order Co")
    return {**office, "v": v, "fol": fol, "lib": lib, "ws": ws}


def pages(s, who):
    """Pages the person can open: a private folder is the employee's own, a File ID needs membership."""
    common = ["/files/mine", "/files/mine/2026-11-17", "/files/tasks", "/files/shared", "/files/library",
              f"/files/library?folder={s['lib'].pk}", "/files/ids", "/files/search?q=order"]  # fmt: skip
    ws = f"/files/ids/{s['ws'].pk}"
    if who == "emp":
        return [*common, f"/files/mine/f/{s['fol'].pk}"]
    return [*common, ws, f"{ws}?tab=members", f"{ws}/physical"]


@pytest.mark.parametrize("who", ["boss", "sub", "emp"])
def test_the_header_order_is_the_same_on_every_files_page(seeded, who):
    c = login(who)
    for url in pages(seeded, who):
        html = c.get(url).content.decode()
        assert 'aria-label="Quick search"' in html and 'aria-label="Files sections"' in html, url
        search = html.index('aria-label="Quick search"')
        strip = html.index('aria-label="Files sections"')
        assert search < strip, url
        if "data-pagehead" in html:  # a nested page: Back and breadcrumb come first
            assert html.index("data-pagehead") < search, url
        body = html[strip:]
        assert "</nav>" in body
        after = body.split("</nav>", 1)[1]
        assert any(m in after for m in ("fm-list", "Files and folders", "<section", "<ul", "<p ")), url


def test_the_tabs_are_the_same_everywhere_and_the_lit_one_follows_the_section(seeded):
    c = login("boss")
    labels = [
        "My storage",
        "Task files",
        "File IDs",
        "Library",
        "Shared with me",
        "People&#x27;s storage",
        "Deleted",
    ]
    expect_on = {"/files/mine": "My storage", "/files/tasks": "Task files", "/files/ids": "File IDs",
                 "/files/library": "Library", "/files/shared": "Shared with me"}  # fmt: skip
    for url in pages(seeded, "boss"):
        strip = c.get(url).content.decode().split('aria-label="Files sections"', 1)[1].split("</nav>", 1)[0]
        for label in labels:
            assert f">{label}</a>" in strip, (url, label)
    for url, on in expect_on.items():
        strip = c.get(url).content.decode().split('aria-label="Files sections"', 1)[1].split("</nav>", 1)[0]
        lit = strip.split('aria-current="page"', 1)[1].split("</a>", 1)[0]
        assert on in lit, (url, lit)  # the lit tab inside the Files strip, not the sidebar
    assert uuid.UUID(str(seeded["ws"].pk))


def test_nested_pages_keep_their_section_lit(seeded):
    c = login("emp")

    def lit(u):
        strip = c.get(u).content.decode().split('aria-label="Files sections"', 1)[1].split("</nav>", 1)[0]
        return strip.split('aria-current="page"', 1)[1].split("</a>", 1)[0]

    assert "My storage" in lit(f"/files/mine/f/{seeded['fol'].pk}")
    assert "My storage" in lit("/files/mine/2026-11-17")


def test_other_people_do_not_get_tabs_they_cannot_use(seeded):
    strip = (
        login("emp")
        .get("/files/mine")
        .content.decode()
        .split('aria-label="Files sections"', 1)[1]
        .split("</nav>", 1)[0]
    )
    assert "People&#x27;s storage" not in strip and ">Deleted</a>" not in strip
