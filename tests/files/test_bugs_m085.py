"""M08.5 bug hunt: leaks through the trail and search, and recipient id spelling."""

import uuid

import pytest
from ots.files import handover, selectors
from ots.files.models import FileVersion

from tests.files.conftest import login, upload_all

pytestmark = pytest.mark.django_db
DATA = bytes(range(256)) * 20


def uploaded(office, name="Sales.xlsx"):
    upload_all(login("emp"), DATA, uuid.uuid4(), office["task"], name=name)
    return FileVersion.objects.order_by("-uploaded_at").first()


def test_a_handed_file_does_not_reveal_the_title_of_a_task_the_receiver_cannot_see(office):
    v = uploaded(office)  # on "Monthly Sales Report", which `other` cannot see
    handover.send(office["sub"], v.pk, [office["other"].pk])
    rows = selectors.trail(office["other"], v.file_id)
    assert rows and not any("Monthly Sales" in e["text"] for e in rows)
    assert "Monthly Sales" not in login("other").get(f"/files/{v.file_id}/trail").content.decode()


def test_search_does_not_probe_hidden_task_titles(office):
    # words past the 40-character cap on the file code's task part exist only in the task title
    from ots.tasks.models import Task

    Task.objects.filter(pk=office["task"].pk).update(
        title="Monthly sales report for the western region zebra"
    )
    v = uploaded(office)
    handover.send(office["sub"], v.pk, [office["other"].pk])
    assert selectors.search(office["other"], "zebra") == []
    assert [x.pk for x in selectors.search(office["emp"], "zebra")] == [v.pk]


def test_recipient_ids_may_be_written_in_capitals_or_without_hyphens(office):
    v = uploaded(office)
    h = handover.send(office["sub"], v.pk, [str(office["other"].pk).upper()])
    assert h.recipients.count() == 1
    h2 = handover.send(office["sub"], v.pk, [office["other"].pk.hex], "again")
    assert h2.recipients.count() == 1


def test_the_same_person_listed_twice_is_one_recipient(office):
    v = uploaded(office)
    pk = str(office["other"].pk)
    h = handover.send(office["sub"], v.pk, [pk, pk.upper()])
    assert h.recipients.count() == 1


def test_a_deactivated_person_cannot_be_sent_a_file(office):
    from ots.core.errors import PermissionDenied

    v = uploaded(office)
    office["other"].is_active = False
    office["other"].save()
    with pytest.raises(PermissionDenied):
        handover.send(office["sub"], v.pk, [office["other"].pk])
