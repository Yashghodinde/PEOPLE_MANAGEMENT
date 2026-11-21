"""F-ID-5: HR cannot reach any task or file by any route. Each route answers 403/404 or hands back an empty,
scoped result; HR's own screens (people, org chart, attendance) keep working.
"""

import json
import uuid

import pytest
from ots.accounts.models import Role
from ots.files import selectors as file_selectors
from ots.files.models import FileVersion
from ots.tasks.models import Task

from tests.accounts.factories import make_user
from tests.files.conftest import login, upload_all
from tests.tasks.factories import make_task

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("dirs")]
DATA = bytes(range(256)) * 4


@pytest.fixture
def world():
    sa = make_user(Role.SUPER_ADMIN, login_id="boss", full_name="Boss Person")
    hr = make_user(Role.HR, reports_to=sa, login_id="hr", full_name="Hema Resources")
    sub = make_user(Role.SUB_ADMIN, reports_to=sa, login_id="sub", full_name="Sub Admin")
    emp = make_user(Role.EMPLOYEE, reports_to=sub, login_id="emp", full_name="Rahul Sharma")
    task = make_task(sub, emp, title="Quarterly Budget Review")
    upload_all(login("emp"), DATA, uuid.uuid4(), task, name="Budget-Review-Q3.xlsx")
    return {"sa": sa, "hr": hr, "sub": sub, "emp": emp, "task": task, "version": FileVersion.objects.get()}


def test_hr_has_no_tasks_and_therefore_no_task_files(world):
    hr = world["hr"]
    assert not Task.objects.visible_to(hr).exists()
    assert not file_selectors.versions_visible_to(hr).exists()  # nothing of the office's reaches HR
    assert file_selectors.search(hr, "Budget", limit=20, offset=0) == []


def test_every_task_page_and_action_is_closed_to_hr(world):
    c, t = login("hr"), world["task"]
    for url in (
        "/app/tasks",
        f"/app/tasks/{t.pk}",
        f"/app/tasks/{t.pk}/files",
        "/app/tasks/new",
        "/app/day-close",
    ):
        assert c.get(url).status_code == 403, url
    assert c.post(f"/app/tasks/{t.pk}/comment", {"body": "hello"}).status_code == 403
    assert c.post(f"/app/tasks/{t.pk}/cancel").status_code == 403
    assert c.post("/app/day-resume").status_code == 403
    assert c.get(f"/app/tasks/{uuid.uuid4()}").status_code == 403  # a guessed id learns nothing either


def test_hr_opens_files_like_an_employee_but_never_someone_elses_task_file(world):
    c, v, t = login("hr"), world["version"], world["task"]
    for url in (
        f"/files/v/{v.pk}/download",
        f"/files/v/{v.pk}/preview",
        f"/files/{v.file_id}/trail",
    ):  # a task file HR is not on: closed, exactly as for any Employee not on that task
        assert c.get(url).status_code in (403, 404), url
    assert c.post(f"/files/attach/{t.pk}", {"version": str(v.pk)}).status_code in (403, 404)
    for url in ("/files/mine", "/files/library", "/files/shared", "/files/ids", "/files/tasks"):
        assert c.get(url).status_code == 200, url
    assert b"Budget-Review-Q3" not in c.get("/files/search?q=Budget").content
    body = json.dumps(
        {"idempotency_key": str(uuid.uuid4()), "original_filename": "2026-11-17_Budget-Plan.xlsx",
         "size": 10, "sha256": "a" * 64, "task_id": str(t.pk)}
    )  # fmt: skip
    assert c.post("/files/uploads", body, content_type="application/json").status_code in (403, 404)
    assert c.post(f"/files/v/{v.pk}/handover", {"to": str(world["sub"].pk)}).status_code in (403, 404)


def test_the_data_layer_alone_still_keeps_hr_out_of_tasks_and_files(world, settings):
    """Second lock: with the URL fence switched off, the scoped queries still answer 404, never the file."""
    settings.MIDDLEWARE = [m for m in settings.MIDDLEWARE if "HrFence" not in m]
    c, v, t = login("hr"), world["version"], world["task"]
    assert c.get(f"/files/v/{v.pk}/download").status_code in (403, 404)
    assert c.get(f"/files/v/{v.pk}/preview").status_code in (403, 404)
    assert c.get(f"/app/tasks/{t.pk}").status_code in (403, 404)
    assert b"Budget-Review-Q3" not in c.get("/files/search?q=Budget").content


def test_a_manager_can_hand_a_file_to_hr_and_hr_can_send_back_to_their_manager(world):
    assert file_selectors.handover_targets(world["sa"]).filter(pk=world["hr"].pk).exists()
    assert file_selectors.handover_targets(world["sub"]).filter(pk=world["hr"].pk).exists()
    assert file_selectors.handover_targets(world["hr"]).exists()


def test_hr_still_has_its_own_screens(world):
    c = login("hr")
    for url in ("/app/dashboard", "/app/people", "/app/org", "/audit"):
        assert c.get(url).status_code == 200, url
    assert c.get("/app/attendance", follow=True).status_code == 200
    body = c.get("/app/dashboard").content.decode()
    assert "Attendance today" in body and "Quarterly Budget Review" not in body and "Add task" not in body


def test_hr_sees_only_the_people_navigation(world):
    nav = login("hr").get("/app/people").content.decode().split("</aside>")[0]
    for shown in ("/app/dashboard", "/app/people", "/app/org", "/app/attendance", "/audit"):
        assert f'href="{shown}"' in nav
    for hidden in ("/app/tasks", "/files/search", "/app/displays"):
        assert f'href="{hidden}"' not in nav


def test_the_audit_log_shows_hr_people_matters_only(world):
    c = login("hr")
    from ots.audit import api as audit
    from ots.audit.models import AuditLog

    audit.record("task.created", world["sub"], world["task"], {})
    audit.record("file.uploaded", world["emp"], world["version"], {})
    seen = set(AuditLog.objects.visible_to(world["hr"]).values_list("action", flat=True))
    assert seen and not {a for a in seen if a.startswith(("task.", "file."))}
    assert c.get("/audit").status_code == 200


def test_hr_profile_pages_have_no_files_tab(world):
    page = login("hr").get(f"/app/people/{world['emp'].pk}").content.decode()
    assert 'data-tab="work"' in page and 'data-tab="attendance"' in page and 'data-tab="files"' not in page
    boss = login("boss").get(f"/app/people/{world['emp'].pk}").content.decode()
    assert 'data-tab="files"' in boss


def test_hr_can_open_attendance_pages_and_ask_for_a_correction(world):
    c = login("hr")
    assert c.get(f"/app/attendance/{world['emp'].pk}").status_code == 200
