"""M08.3: the files section of a task page."""

import uuid

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from ots.files.models import FileState, FileVersion

from tests.files.conftest import login, upload_all

pytestmark = pytest.mark.django_db
DATA = bytes(range(256)) * 12


def panel(c, task, query=""):
    return c.get(f"/app/tasks/{task.pk}/files{query}")


def test_the_task_page_loads_the_files_section_after_the_page(office):
    body = login("emp").get(f"/app/tasks/{office['task'].pk}").content.decode()
    assert f'hx-get="/app/tasks/{office["task"].pk}/files"' in body and 'hx-trigger="intersect once"' in body


def test_the_uploader_scripts_load_for_signed_in_people(office):
    body = login("emp").get("/app/dashboard").content.decode()
    for name in ("sha256.js", "upload-core.js", "uploader.js"):
        assert f"js/{name}" in body
    from django.test import Client

    assert "uploader.js" not in Client().get("/login").content.decode()


@pytest.mark.parametrize("who", ["emp", "sub", "boss"])
def test_people_who_may_upload_get_the_uploader(office, who):
    body = panel(login(who), office["task"]).content.decode()
    assert 'x-data="uploader"' in body and f'data-task-id="{office["task"].pk}"' in body


def test_an_employee_is_not_offered_a_file_kind_choice_but_a_manager_is(office):
    emp = panel(login("emp"), office["task"]).content.decode()
    assert 'data-default-relation="SUBMISSION"' in emp and "<select" not in emp
    sub = panel(login("sub"), office["task"]).content.decode()
    assert 'data-default-relation="DELIVERABLE"' in sub and "Reference</option>" in sub


def test_someone_outside_the_task_gets_a_404_and_no_files(office):
    upload_all(login("emp"), DATA, uuid.uuid4(), office["task"], name="secret-plan.xlsx")
    r = panel(login("other"), office["task"])
    assert r.status_code == 404 and "secret-plan" not in r.content.decode()


def test_a_cancelled_task_shows_files_but_no_uploader(office):
    from ots.tasks import services as task_services

    upload_all(login("emp"), DATA, uuid.uuid4(), office["task"], name="done.xlsx")
    task_services.cancel_task(office["sub"], office["task"].pk, "not needed")
    body = panel(login("sub"), office["task"]).content.decode()
    assert "done.xlsx" in body and 'x-data="uploader"' not in body


def test_an_uploaded_file_is_listed_with_not_yet_off_site_until_archived(office):
    upload_all(login("emp"), DATA, uuid.uuid4(), office["task"], name="Sales.xlsx")
    c = login("sub")
    body = panel(c, office["task"]).content.decode()
    assert (
        "Sales.xlsx" in body and "Not yet off-site" in body and "Version 1" in body and "Rahul Sharma" in body
    )
    assert "3.0\xa0KB" in body or "3\xa0KB" in body
    FileVersion.objects.update(state=FileState.ARCHIVED)
    done = panel(c, office["task"]).content.decode()
    assert "Not yet off-site" not in done and "Off-site" in done


def test_the_list_refreshes_itself_only_while_something_is_pending(office):
    upload_all(login("emp"), DATA, uuid.uuid4(), office["task"])
    c = login("sub")
    assert "every 15s" in panel(c, office["task"], "?list=1").content.decode()
    FileVersion.objects.update(state=FileState.ARCHIVED)
    after = panel(c, office["task"], "?list=1").content.decode()
    assert "every 15s" not in after and "ots:file-uploaded" in after


def test_the_list_fragment_is_only_the_list(office):
    upload_all(login("emp"), DATA, uuid.uuid4(), office["task"])
    frag = panel(login("sub"), office["task"], "?list=1").content.decode()
    assert 'id="files-list"' in frag and 'x-data="uploader"' not in frag and "<html" not in frag
    assert frag.count('id="files-panel"') == 0


def test_only_the_newest_version_offers_a_new_version_button(office):
    c = login("emp")
    upload_all(c, DATA, uuid.uuid4(), office["task"])
    from ots.files.models import File

    upload_all(c, DATA + b"2", uuid.uuid4(), office["task"], file_id=str(File.objects.get().pk))
    body = panel(c, office["task"]).content.decode()
    assert (
        body.count("Upload a new version") == 1
        and body.count("Version 2") == 1
        and body.count("Version 1") == 1
    )


def test_the_panel_query_count_does_not_grow_with_the_number_of_files(office):
    c = login("sub")
    emp = login("emp")
    upload_all(emp, DATA, uuid.uuid4(), office["task"], name="a.xlsx")
    with CaptureQueriesContext(connection) as few:
        panel(c, office["task"])
    for i in range(8):
        upload_all(emp, DATA + bytes([i]), uuid.uuid4(), office["task"], name=f"f{i}.xlsx")
    with CaptureQueriesContext(connection) as many:
        panel(c, office["task"])
    assert len(many) == len(few)


def test_a_manager_sees_the_names_of_files_but_the_panel_needs_login(office):
    from django.test import Client

    assert Client().get(f"/app/tasks/{office['task'].pk}/files").status_code == 302
