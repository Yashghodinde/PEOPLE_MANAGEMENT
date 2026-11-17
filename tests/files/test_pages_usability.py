"""QA pass: no dead links in the sidebar, every files page has a title and heading, tables label their cells."""

import re
import uuid

import pytest
from ots.ui.nav import items_for

from tests.files.conftest import login, upload_all

pytestmark = pytest.mark.django_db
DATA = bytes(range(256)) * 20


@pytest.mark.parametrize("role", ["SUPER_ADMIN", "SUB_ADMIN", "EMPLOYEE"])
def test_the_sidebar_has_no_dead_links(role):
    items = items_for(role)
    assert items and all(i.path.startswith("/") for i in items), [i.label for i in items if i.path == "#"]
    assert "Files" in [i.label for i in items]


def test_the_files_item_is_highlighted_on_every_files_page(office):
    c = login("boss")
    for path in ("/files/search", "/files/library"):
        html = c.get(path).content.decode()
        assert re.search(r'href="/files/search"[^>]*aria-current="page"', html), path


def test_every_files_page_has_a_title_and_a_heading(office):
    upload_all(login("emp"), DATA, uuid.uuid4(), office["task"], name="a.xlsx")
    from ots.files.models import FileVersion

    v = FileVersion.objects.get()
    c = login("boss")
    pages = {
        "/files/search": "Files",
        "/files/library": "Library",
        f"/files/v/{v.pk}/handover": "Send file",
        f"/files/{v.file_id}/trail": "File history",
    }
    for path, title in pages.items():
        html = c.get(path).content.decode()
        assert f"<title>{title} · OTS</title>" in html, path
        assert re.search(rf"<h1[^>]*>\s*{title}\s*</h1>", html), path


def test_table_cells_carry_their_column_name_so_phones_can_stack_them(office):
    html = login("boss").get("/app/tasks/all").content.decode()
    labels = set(re.findall(r'<td data-label="([^"]*)"', html))
    assert {"Code", "Title", "Due", "Status"} <= labels
