"""D-095 D1: seeing a task does not open its files. A file belongs to the exchange that made it: the people in it,
their managers up the chain and the Super Admin. Two employees on one task never see each other's files."""

import uuid

import pytest
from ots.accounts.models import Role
from ots.files import selectors
from ots.files.models import FileVersion
from ots.tasks.models import TaskAssignment

from tests.accounts.factories import make_user
from tests.files.conftest import login, upload_all
from tests.tasks.factories import make_task

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("dirs")]
DATA = bytes((i * 7) % 256 for i in range(1200))


@pytest.fixture
def world():
    sa = make_user(Role.SUPER_ADMIN, login_id="boss")
    director = make_user(Role.SUB_ADMIN, reports_to=sa, login_id="dir")
    manager = make_user(Role.SUB_ADMIN, reports_to=director, login_id="mgr")
    other_mgr = make_user(Role.SUB_ADMIN, reports_to=director, login_id="mgr2")
    e1 = make_user(Role.EMPLOYEE, reports_to=manager, login_id="e1")
    e2 = make_user(Role.EMPLOYEE, reports_to=manager, login_id="e2")
    outsider = make_user(Role.EMPLOYEE, reports_to=other_mgr, login_id="e3")
    task = make_task(manager, e1, e2, title="Shared Task")
    a1 = TaskAssignment.objects.get(task=task, user=e1)
    a2 = TaskAssignment.objects.get(task=task, user=e2)
    return dict(sa=sa, director=director, manager=manager, other_mgr=other_mgr, e1=e1, e2=e2,
                outsider=outsider, task=task, a1=a1, a2=a2)  # fmt: skip


def submit(w, who, assignment, name):
    upload_all(login(who), DATA + name.encode(), uuid.uuid4(), w["task"], name=name,
               assignment_id=str(assignment.pk), relation="SUBMISSION")  # fmt: skip
    return FileVersion.objects.filter(file__display_name__startswith=name.split(".")[0]).get()


def can(user, v):
    return selectors.versions_visible_to(user).filter(pk=v.pk).exists()


def test_an_employee_never_sees_another_employees_submission(world):
    w = world
    v1 = submit(w, "e1", w["a1"], "e1-work.xlsx")
    v2 = submit(w, "e2", w["a2"], "e2-work.xlsx")
    assert can(w["e1"], v1) and not can(w["e1"], v2)
    assert can(w["e2"], v2) and not can(w["e2"], v1)
    assert login("e2").get(f"/files/v/{v1.pk}/download").status_code == 404


def test_the_manager_chain_and_the_boss_see_both(world):
    w = world
    v1 = submit(w, "e1", w["a1"], "e1-work.xlsx")
    for who in ("manager", "director", "sa"):
        assert can(w[who], v1), who


def test_a_manager_outside_the_chain_and_a_stranger_see_nothing(world):
    w = world
    v1 = submit(w, "e1", w["a1"], "e1-work.xlsx")
    assert not can(w["other_mgr"], v1) and not can(w["outsider"], v1)


def test_a_reference_sheet_from_the_creator_goes_to_every_assignee(world):
    w = world
    upload_all(login("mgr"), DATA + b"ref", uuid.uuid4(), w["task"], name="brief.pdf", relation="REFERENCE")
    v = FileVersion.objects.get(file__display_name__startswith="brief")
    assert can(w["e1"], v) and can(w["e2"], v) and not can(w["outsider"], v)


def test_a_file_the_manager_gave_one_employee_stays_with_that_employee(world):
    w = world
    upload_all(login("mgr"), DATA + b"for1", uuid.uuid4(), w["task"], name="for-e1.pdf",
               assignment_id=str(w["a1"].pk), relation="REFERENCE")  # fmt: skip
    v = FileVersion.objects.get(file__display_name__startswith="for-e1")
    assert can(w["e1"], v) and not can(w["e2"], v)


def test_the_task_files_page_lists_only_my_exchange(world):
    w = world
    submit(w, "e1", w["a1"], "e1-work.xlsx")
    submit(w, "e2", w["a2"], "e2-work.xlsx")
    page = login("e1").get(f"/files/tasks/{w['task'].pk}").content.decode()
    assert "e1-work" in page and "e2-work" not in page
    panel = login("e2").get(f"/app/tasks/{w['task'].pk}/files").content.decode()
    assert "e2-work" in panel and "e1-work" not in panel
