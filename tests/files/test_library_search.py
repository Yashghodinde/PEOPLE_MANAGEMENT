"""M08.5: library, attach-existing, search (section 7)."""

import uuid

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from ots.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from ots.files import library, selectors
from ots.files.models import Blob, File, FileVersion, FileVisibility, LibraryFolder, TaskFile
from ots.tasks.models import TaskEvent

from tests.files.conftest import login, upload_all
from tests.files.factories import make_file

pytestmark = pytest.mark.django_db
DATA = bytes(range(256)) * 20


def uploaded(office, who="emp", name="Sales.xlsx"):
    upload_all(login(who), DATA + name.encode(), uuid.uuid4(), office["task"], name=name)
    return FileVersion.objects.order_by("-uploaded_at").first()


# --- library --------------------------------------------------------------------------------------------------


def test_only_the_super_admin_manages_the_library(office):
    for who in ("sub", "emp"):
        with pytest.raises(PermissionDenied):
            library.create_folder(office[who], "Policies")
    f = library.create_folder(office["sa"], "  Policies  ")
    assert f.name == "Policies"
    with pytest.raises(Conflict):
        library.create_folder(office["sa"], "Policies")
    child = library.create_folder(office["sa"], "Policies", f.pk)  # same name under another parent is fine
    assert child.parent_id == f.pk


@pytest.mark.parametrize("bad", ["", "   ", "a/b", "a\\b", "x" * 121])
def test_bad_folder_names_are_refused(office, bad):
    with pytest.raises(ValidationFailed):
        library.create_folder(office["sa"], bad)


def test_rename_and_move(office):
    fo = library.create_folder(office["sa"], "HR")
    library.rename_folder(office["sa"], fo.pk, "People")
    assert LibraryFolder.objects.get(pk=fo.pk).name == "People"
    f, _ = make_file(office["sa"])
    with pytest.raises(NotFound):  # a task file is not a library file
        library.move_file(office["sa"], f.pk, fo.pk)
    f.visibility = FileVisibility.LIBRARY
    f.save()
    library.move_file(office["sa"], f.pk, fo.pk)
    assert File.objects.get(pk=f.pk).library_folder_id == fo.pk
    with pytest.raises(PermissionDenied):
        library.move_file(office["emp"], f.pk, None)


def test_everyone_can_read_the_library_and_only_the_boss_sees_the_forms(office):
    fo = library.create_folder(office["sa"], "Policies")
    f, v = make_file(office["sa"])
    f.visibility, f.library_folder = FileVisibility.LIBRARY, fo
    f.save()
    for who, form in (("emp", False), ("boss", True)):
        r = login(who).get(f"/files/library?folder={fo.pk}")
        assert r.status_code == 200 and f.display_name.encode() in r.content
        assert (b"New folder" in r.content) is form
    assert login("emp").get(f"/files/v/{v.pk}/download").status_code in (200, 503)  # library: visible
    assert login("emp").post("/files/library/folders", {"name": "x"}).status_code == 403
    assert login("emp").get("/files/library?folder=" + str(uuid.uuid4())).status_code == 404


# --- attach existing ------------------------------------------------------------------------------------------


def test_attach_creates_a_reference_link_and_no_blob_or_version(office):
    v = uploaded(office)
    from tests.tasks.factories import make_task  # noqa: PLC0415

    other_task = make_task(office["sub"], office["other"], title="Another job")
    blobs, versions = Blob.objects.count(), FileVersion.objects.count()
    link, created = library.attach_existing(office["sub"], other_task.pk, v.pk)
    assert created and link.relation == "REFERENCE" and link.version_id == v.pk
    assert (Blob.objects.count(), FileVersion.objects.count()) == (blobs, versions)
    assert TaskEvent.objects.filter(task=other_task, event_type="file.attached").count() == 1
    again, created = library.attach_existing(office["sub"], other_task.pk, v.pk)
    assert not created and again.pk == link.pk and TaskFile.objects.filter(task=other_task).count() == 1


def test_employees_cannot_attach_and_nobody_attaches_what_they_cannot_see(office):
    v = uploaded(office)
    with pytest.raises(PermissionDenied):
        library.attach_existing(office["emp"], office["task"].pk, v.pk)
    _, hidden = make_file(office["sa"])
    with pytest.raises(NotFound):
        library.attach_existing(office["sub"], office["task"].pk, hidden.pk)


def test_attach_from_the_search_page(office):
    v = uploaded(office)
    from tests.tasks.factories import make_task  # noqa: PLC0415

    t = make_task(office["sub"], office["other"], title="Another job")
    c = login("sub")
    page = c.get(f"/files/search?attach={t.pk}&q=sales")
    assert b"Attach to this task" in page.content
    assert c.post(f"/files/attach/{t.pk}", {"version": str(v.pk)}).status_code == 302
    assert TaskFile.objects.filter(task=t, version=v, relation="REFERENCE").exists()
    assert b"Attach to this task" not in login("emp").get(f"/files/search?attach={t.pk}").content


# --- search ---------------------------------------------------------------------------------------------------


def test_search_matches_name_code_task_title_and_uploader(office):
    v = uploaded(office, name="Quarterly Budget.xlsx")
    for q in ("budget", "QUARTERLY bud", v.file_code[:12], "monthly sales", "rahul", "rahul budget"):
        assert [x.pk for x in selectors.search(office["sa"], q)] == [v.pk], q
    assert selectors.search(office["sa"], "nothing-like-this") == []
    assert selectors.search(office["sa"], "rahul zzz") == []  # every word must match


def test_search_treats_percent_and_underscore_as_plain_characters(office):
    uploaded(office, name="plain.xlsx")
    assert selectors.search(office["sa"], "%") == []
    assert selectors.search(office["sa"], "p_ain") == []


def test_search_is_scoped_to_what_the_viewer_may_open(office):
    uploaded(office, name="Secret Plan.xlsx")
    assert len(selectors.search(office["emp"], "secret")) == 1
    assert selectors.search(office["other"], "secret") == []
    assert login("other").get("/files/search?q=secret").content.count(b"Secret Plan") == 0


def test_search_filters(office):
    a = uploaded(office, name="a.pdf")
    b = uploaded(office, who="sub", name="b.xlsx")
    s = office["sa"]
    assert [x.pk for x in selectors.search(s, extension="pdf")] == [a.pk]
    assert [x.pk for x in selectors.search(s, uploader=office["sub"].pk)] == [b.pk]
    assert {x.pk for x in selectors.search(s, task=office["task"].pk)} == {a.pk, b.pk}
    assert selectors.search(s, state="ARCHIVED") == []
    assert len(selectors.search(s, relation="SUBMISSION")) == 2
    assert selectors.search(s, relation="REFERENCE") == []
    assert len(selectors.search(s, date_from="2000-01-01", date_to="2999-01-01")) == 2
    assert login("boss").get("/files/search?from=nope").status_code == 422


def test_search_is_paged_and_never_unbounded(office):
    for i in range(3):
        uploaded(office, name=f"f{i}.txt")
    assert len(selectors.search(office["sa"], limit=2)) == 2
    assert len(selectors.search(office["sa"], limit=2, offset=2)) == 1
    assert len(selectors.search(office["sa"], limit=10**6)) == 3  # capped at 200


def test_search_query_count_does_not_grow_with_results(office):
    uploaded(office, name="one.txt")
    with CaptureQueriesContext(connection) as small:
        login("boss").get("/files/search?q=txt")
    for i in range(5):
        uploaded(office, name=f"more{i}.txt")
    with CaptureQueriesContext(connection) as big:
        login("boss").get("/files/search?q=txt")
    assert len(big) <= len(small) + 1
