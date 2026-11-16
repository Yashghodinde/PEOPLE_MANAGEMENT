"""M08.5: handovers (section 6): who may send to whom, first open recorded once, the bell, the trail events."""

import uuid

import pytest
from ots.core.errors import NotFound, PermissionDenied, ValidationFailed
from ots.files import handover
from ots.files.models import FileHandover, FileHandoverRecipient, FileVersion
from ots.notifications.models import Kind, Notification
from ots.tasks.models import TaskEvent

from tests.files.conftest import login, upload_all
from tests.files.factories import make_file

pytestmark = pytest.mark.django_db
DATA = bytes(range(256)) * 20


def uploaded(office, who="emp"):
    upload_all(login(who), DATA, uuid.uuid4(), office["task"], name="Sales.xlsx")
    return FileVersion.objects.order_by("-uploaded_at").first()


def test_an_employee_can_send_to_their_manager_and_the_person_is_told(office):
    v = uploaded(office)
    h = handover.send(office["emp"], v.pk, [office["sub"].pk], "please check")
    assert h.note == "please check" and h.task_id == office["task"].pk
    assert list(h.recipients.values_list("to_user_id", flat=True)) == [office["sub"].pk]
    bell = Notification.objects.get(user=office["sub"], kind=Kind.FILE_HANDED)
    assert "Rahul Sharma" in bell.title and bell.body == "Sales.xlsx"
    assert not Notification.objects.filter(user=office["emp"], kind=Kind.FILE_HANDED).exists()
    assert TaskEvent.objects.filter(task=office["task"], event_type="file.handed_over").count() == 1


def test_an_employee_cannot_send_to_a_stranger_and_nothing_is_kept(office):
    v = uploaded(office)
    with pytest.raises(PermissionDenied):
        handover.send(office["emp"], v.pk, [office["sub"].pk, office["other"].pk])
    assert not FileHandover.objects.exists() and not FileHandoverRecipient.objects.exists()


def test_an_admin_can_send_to_anyone_and_the_receiver_can_then_open_it(office):
    v = uploaded(office)
    assert login("other").get(f"/files/v/{v.pk}/download").status_code == 404
    handover.send(office["sub"], v.pk, [office["other"].pk])
    assert login("other").get(f"/files/v/{v.pk}/download").status_code == 200


def test_first_open_is_recorded_once(office):
    v = uploaded(office)
    handover.send(office["sub"], v.pk, [office["other"].pk])
    c = login("other")
    assert c.get(f"/files/v/{v.pk}/download").status_code == 200
    r = FileHandoverRecipient.objects.get(to_user=office["other"])
    first = r.first_opened_at
    assert first is not None
    c.get(f"/files/v/{v.pk}/download")
    c.get(f"/files/v/{v.pk}/preview")
    r.refresh_from_db()
    assert r.first_opened_at == first
    assert TaskEvent.objects.filter(event_type="file.handover_opened").count() == 1


def test_someone_else_opening_it_does_not_mark_the_recipient(office):
    v = uploaded(office)
    handover.send(office["sub"], v.pk, [office["other"].pk])
    login("boss").get(f"/files/v/{v.pk}/download")
    assert FileHandoverRecipient.objects.get().first_opened_at is None


def test_a_double_send_is_one_handover(office):
    v = uploaded(office)
    a = handover.send(office["sub"], v.pk, [office["other"].pk], "x")
    b = handover.send(office["sub"], v.pk, [office["other"].pk], "x")
    assert a.pk == b.pk and FileHandover.objects.count() == 1
    assert Notification.objects.filter(kind=Kind.FILE_HANDED).count() == 1


def test_bad_input_is_refused(office):
    v = uploaded(office)
    with pytest.raises(ValidationFailed):
        handover.send(office["sub"], v.pk, [])
    with pytest.raises(ValidationFailed):
        handover.send(office["sub"], v.pk, ["not-a-uuid"])
    with pytest.raises(ValidationFailed):
        handover.send(office["sub"], v.pk, [office["other"].pk], "x" * 1001)
    with pytest.raises(PermissionDenied):  # nobody sends a file to themselves
        handover.send(office["sub"], v.pk, [office["sub"].pk])


def test_you_cannot_send_a_file_you_cannot_see(office):
    _, v = make_file(office["sa"])
    with pytest.raises(NotFound):
        handover.send(office["emp"], v.pk, [office["sub"].pk])


def test_a_library_file_handover_has_no_task_and_no_bell_but_is_recorded(office):
    _, v = make_file(office["sa"])
    h = handover.send(office["sa"], v.pk, [office["emp"].pk])
    assert h.task_id is None and not Notification.objects.exists()


def test_the_send_page_works_without_javascript(office):
    v = uploaded(office)
    c = login("sub")
    page = c.get(f"/files/v/{v.pk}/handover")
    assert page.status_code == 200 and b"Send to" in page.content and b"<title>Send file" in page.content
    r = c.post(f"/files/v/{v.pk}/handover", {"recipients": [str(office["other"].pk)], "note": "hi"})
    assert r.status_code == 302 and FileHandover.objects.count() == 1
    r = c.post(f"/files/v/{v.pk}/handover", {"note": "hi"})
    assert r.status_code == 422 and b"at least one" in r.content


# --- the trail ------------------------------------------------------------------------------------------------


def test_the_trail_merges_everything_newest_first(office, remote):
    from tests.files.conftest import drain

    v = uploaded(office)
    drain(remote)
    handover.send(office["sub"], v.pk, [office["other"].pk], "look")
    login("other").get(f"/files/v/{v.pk}/download")
    rows = login("boss").get(f"/files/{v.file_id}/trail")
    assert rows.status_code == 200
    text = rows.content.decode()
    for needle in ("Rahul Sharma uploaded version 1", "stored off-site", "Sub Admin sent version 1 to Other Person",
                   "Other Person opened it for the first time", "Other Person downloaded it", "Linked to task"):  # fmt: skip
        assert needle in text, needle
    from ots.files.selectors import trail

    ats = [e["at"] for e in trail(office["sa"], v.file_id)]
    assert ats == sorted(ats, reverse=True)


def test_the_trail_shows_storage_problems(office):
    from django.db import transaction
    from ots.audit import api as audit

    v = uploaded(office)
    with transaction.atomic():
        audit.record("file.missing", None, v, {"file_code": v.file_code})
    from ots.files.selectors import trail

    assert "Missing from SharePoint" in [e["text"] for e in trail(office["sa"], v.file_id)]


def test_the_trail_is_not_for_people_who_cannot_see_the_file(office):
    v = uploaded(office)
    assert login("other").get(f"/files/{v.file_id}/trail").status_code == 404


def test_an_employee_sees_only_their_own_downloads_in_the_trail(office):
    v = uploaded(office)
    login("sub").get(f"/files/v/{v.pk}/download")
    login("emp").get(f"/files/v/{v.pk}/download")
    from ots.files.selectors import trail

    seen = [e["text"] for e in trail(office["emp"], v.file_id) if e["kind"] == "access"]
    assert seen == ["Rahul Sharma downloaded it"]
