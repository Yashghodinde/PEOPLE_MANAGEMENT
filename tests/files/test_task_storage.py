"""IMP-03: task files by date, then task, then document, and the exchange timeline (FTS-05 to FTS-07)."""

import datetime
import uuid

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from freezegun import freeze_time
from ots.accounts.models import Role
from ots.files import taskstorage
from ots.files.models import File, FileVersion
from ots.tasks import services as task_services

from tests.accounts.factories import make_user
from tests.files.conftest import body, login, upload_all
from tests.tasks.factories import make_task

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("dirs")]
D1, D2, D3 = (bytes((i * k) % 256 for i in range(900)) for k in (3, 5, 7))


def put(who, task, data, name="2026-11-17_Budget-plan.xlsx", relation="SUBMISSION", file_id=None):
    extra = {"relation": relation}
    if file_id:
        extra["file_id"] = str(file_id)
    upload_all(login(who), data, uuid.uuid4(), task, name=name, **extra)
    return FileVersion.objects.order_by("-uploaded_at", "-pk").first()


def exchange(office):
    """A manager shares v1, the employee returns v2, the manager asks for changes, then uploads v3."""
    t = office["task"]
    with freeze_time("2026-11-17 05:00:00"):
        v1 = put("sub", t, D1, relation="DELIVERABLE")
    with freeze_time("2026-11-17 06:00:00"):
        v2 = put("emp", t, D2, file_id=v1.file_id)
    with freeze_time("2026-11-17 06:30:00"):
        a = t.assignments.get(user=office["emp"])
        task_services.transition(a.pk, "start", office["emp"])
        task_services.transition(a.pk, "submit", office["emp"], note="Here is the revised sheet.")
    with freeze_time("2026-11-17 07:00:00"):
        task_services.transition(a.pk, "request_changes", office["sub"], feedback="Please fix the totals.")
    with freeze_time("2026-11-17 08:00:00"):
        v3 = put("sub", t, D3, file_id=v1.file_id, relation="DELIVERABLE")
    return v1, v2, v3


def test_versions_of_one_document_are_one_file_and_labels_follow_who_uploaded(office):
    v1, v2, v3 = exchange(office)
    assert v1.file_id == v2.file_id == v3.file_id and [v.version_number for v in (v1, v2, v3)] == [1, 2, 3]
    docs = taskstorage.document_groups(office["sub"], office["task"])
    assert len(docs) == 1
    assert [(r.version.version_number, r.action) for r in docs[0].rows] == [
        (1, "Shared"),
        (2, "Revised"),
        (3, "Corrections"),
    ]


def test_a_first_version_by_an_assignee_is_submitted_and_a_reference_is_a_reference(office):
    t = office["task"]
    ref = put("sub", t, D1, name="2026-11-17_Brief.pdf", relation="REFERENCE")
    mine = put("emp", t, D2, name="2026-11-17_Draft-sheet.xlsx")
    rows = {r.version.pk: r.action for d in taskstorage.document_groups(office["sub"], t) for r in d.rows}
    assert rows == {ref.pk: "Reference", mine.pk: "Submitted"}


def test_each_row_shows_who_when_the_names_note_state_and_badge(office):
    v1, _, _ = exchange(office)
    office["emp"].designation = "Accounts Executive"
    office["emp"].save(update_fields=["designation"])
    page = login("sub").get(f"/files/tasks/{office['task'].pk}").content.decode()
    assert "Rahul Sharma (Employee, Accounts Executive)" in page and "Sub Admin (Sub-Admin)" in page
    assert "Version 1" in page and "Version 3" in page and v1.file_code in page
    assert "Not yet off-site" in page or "Off-site" in page


def test_the_timeline_orders_the_whole_exchange_newest_first(office):
    exchange(office)
    tl = taskstorage.task_timeline(office["sub"], office["task"])
    titles = [e["title"] for e in tl]
    assert titles == [
        "Sub Admin corrections 2026-11-17_Budget-plan.xlsx (version 3)",
        "Sub Admin asked for changes",
        "Rahul Sharma submitted their work",
        "Rahul Sharma revised 2026-11-17_Budget-plan.xlsx (version 2)",
        "Sub Admin shared 2026-11-17_Budget-plan.xlsx (version 1)",
    ]
    assert [e["note"] for e in tl][1] == "Please fix the totals."
    assert FileVersion.objects.count() == 3  # a change request never makes a version


def test_a_change_request_without_a_file_creates_no_version(office):
    t = office["task"]
    put("sub", t, D1, relation="DELIVERABLE")
    a = t.assignments.get(user=office["emp"])
    task_services.transition(a.pk, "start", office["emp"])
    task_services.transition(a.pk, "submit", office["emp"], note="All done, the sheet is attached.")
    task_services.transition(a.pk, "request_changes", office["sub"], feedback="Again, please.")
    assert FileVersion.objects.count() == 1
    assert any(e["title"].endswith("asked for changes") for e in taskstorage.task_timeline(office["sub"], t))


def test_handovers_appear_when_sent_and_when_opened(office):
    from ots.files import handover

    v1, _, _ = exchange(office)
    handover.send(office["sub"], v1.pk, [office["emp"].pk])
    login("emp").get(f"/files/v/{v1.pk}/download")
    titles = [e["title"] for e in taskstorage.task_timeline(office["sub"], office["task"])]
    assert any("sent 2026-11-17_Budget-plan.xlsx to Rahul Sharma" in t for t in titles)
    assert any("Rahul Sharma opened 2026-11-17_Budget-plan.xlsx" in t for t in titles)


def test_old_versions_still_open_unchanged(office):
    v1, v2, v3 = exchange(office)
    for v, data in ((v1, D1), (v2, D2), (v3, D3)):
        r = login("sub").get(f"/files/v/{v.pk}/download")
        assert r.status_code == 200 and body(r) == data


# --- /files/tasks: date, task, document ------------------------------------------------------------------------


def test_grouping_across_two_days_and_two_tasks_with_a_task_spanning_days(office):
    t1, t2 = office["task"], make_task(office["sub"], office["emp"], title="Second task")
    with freeze_time("2026-11-16 06:00:00"):
        put("emp", t1, D1, name="2026-11-16_One-a.xlsx")
    with freeze_time("2026-11-17 06:00:00"):
        put("emp", t1, D2, name="2026-11-17_One-b.xlsx")
        put("emp", t2, D3, name="2026-11-17_Two-a.xlsx")
    days, nxt = taskstorage.task_storage(office["emp"])
    assert [d.day for d in days] == [datetime.date(2026, 11, 17), datetime.date(2026, 11, 16)] and nxt is None
    on17 = {tb.task.pk: [d.file.display_name for d in tb.documents] for tb in days[0].tasks}
    assert on17 == {t1.pk: ["2026-11-17_One-b.xlsx"], t2.pk: ["2026-11-17_Two-a.xlsx"]}
    assert [d.file.display_name for d in days[1].tasks[0].documents] == ["2026-11-16_One-a.xlsx"]
    assert days[1].tasks[0].task.pk == t1.pk  # the same task under both days


def test_a_week_at_a_time_and_older_days_by_before(office):
    t = office["task"]
    for i in range(9):
        with freeze_time(f"2026-11-{i + 1:02d} 06:00:00"):
            put("emp", t, D1 + bytes([i]), name=f"2026-11-{i + 1:02d}_Day-{i}.xlsx")
    days, nxt = taskstorage.task_storage(office["emp"])
    assert len(days) == 7 and days[0].day == datetime.date(2026, 11, 9) and nxt == datetime.date(2026, 11, 3)
    more, nxt2 = taskstorage.task_storage(office["emp"], nxt)
    assert [d.day for d in more] == [datetime.date(2026, 11, 2), datetime.date(2026, 11, 1)] and nxt2 is None


def test_viewer_scoping_unrelated_people_hr_and_the_team_rule(office):
    put("emp", office["task"], D1)
    stranger = make_user(Role.EMPLOYEE, reports_to=office["sub"], login_id="stranger")
    other_mgr = make_user(Role.SUB_ADMIN, reports_to=office["sa"], login_id="mgr2")
    make_user(Role.HR, reports_to=office["sa"], login_id="hrx")
    assert taskstorage.task_storage(stranger)[0] == [] and taskstorage.task_storage(other_mgr)[0] == []
    assert taskstorage.task_storage(office["sub"])[0] and taskstorage.task_storage(office["sa"])[0]
    assert login("hrx").get("/files/tasks").status_code == 403
    assert login("stranger").get(f"/files/tasks/{office['task'].pk}").status_code == 404


def test_personal_and_voice_files_never_show_in_task_storage(office):
    upload_all(login("emp"), D1, uuid.uuid4(), None, name="2026-11-17_Private.txt", scope="personal")
    upload_all(login("emp"), D2, uuid.uuid4(), office["task"], name="voice.webm", relation="VOICE")
    assert taskstorage.task_storage(office["emp"])[0] == []


def test_the_page_query_count_is_flat_for_many_versions(office):
    t = office["task"]
    for i in range(3):
        put("emp", t, D1 + bytes([i]), name=f"2026-11-17_Small-{i}.xlsx")
    c = login("emp")
    with CaptureQueriesContext(connection) as small:
        assert c.get("/files/tasks").status_code == 200
    for i in range(3, 40):
        put("emp", t, D2 + bytes([i]), name=f"2026-11-17_Many-{i}.xlsx")
    c = login("emp")
    with CaptureQueriesContext(connection) as big:
        assert c.get("/files/tasks").status_code == 200
    assert len(big) == len(small)
    assert File.objects.count() == 40


# --- IMP-03.3: the same-name hint and the note ---------------------------------------------------------------------


def check(who, name, task):
    import json

    return (
        login(who)
        .post(
            "/files/check-name",
            json.dumps({"name": name, "task_id": str(task.pk)}),
            content_type="application/json",
        )
        .json()
    )


def test_check_name_points_at_a_document_with_the_same_name_and_its_next_version(office):
    v1 = put("sub", office["task"], D1, relation="DELIVERABLE")
    put("emp", office["task"], D2, file_id=v1.file_id)
    got = check("emp", "2026-11-17_BUDGET-plan.XLSX", office["task"])  # capitals do not matter
    assert got["ok"] and got["same"] == {
        "file_id": str(v1.file_id),
        "name": v1.file.display_name,
        "next_version": 3,
    }
    assert check("emp", "2026-11-17_Something-else.xlsx", office["task"])["same"] is None


def test_the_hint_never_reaches_someone_who_cannot_see_the_document(office):
    put("sub", office["task"], D1, relation="DELIVERABLE")
    stranger = make_user(Role.EMPLOYEE, reports_to=office["sub"], login_id="stranger")
    assert check("stranger", "2026-11-17_Budget-plan.xlsx", office["task"])["same"] is None and stranger.pk


def test_yes_makes_version_two_of_the_same_file_and_no_makes_a_new_document(office):
    t = office["task"]
    v1 = put("sub", t, D1, relation="DELIVERABLE")
    yes = put("emp", t, D2, file_id=v1.file_id)  # the page sets file_id only after the person says Yes
    no = put("emp", t, D3, name="2026-11-17_Budget-plan.xlsx")  # No: the same name, a separate document
    assert yes.file_id == v1.file_id and yes.version_number == 2
    assert no.file_id != v1.file_id and no.version_number == 1


def test_the_note_is_stored_with_the_version_and_shown(office):
    t = office["task"]
    upload_all(
        login("emp"), D1, uuid.uuid4(), t, name="2026-11-17_Notes-sheet.xlsx", note="  Totals   fixed  "
    )
    v = FileVersion.objects.get()
    assert v.note == "Totals fixed"
    assert "Note: Totals fixed" in login("sub").get(f"/files/tasks/{t.pk}").content.decode()
    assert "Totals fixed" in [e["note"] for e in taskstorage.task_timeline(office["sub"], t)]
