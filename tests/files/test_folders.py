"""IMP-14.2: logical folders. Nested, sibling names unique, privacy, depth cap, moves, no loops, audited."""

import datetime
import uuid

import pytest
from ots.accounts.models import Role
from ots.audit.models import AuditLog
from ots.core.errors import Conflict, NotFound, ValidationFailed
from ots.files import folders
from ots.files.models import File, FileVisibility, Folder

from tests.accounts.factories import make_user
from tests.files.test_personal import mine_upload

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("dirs")]


def test_create_nested_folder_under_the_open_one(office):
    emp = office["emp"]
    finance = folders.create_personal(emp, "Finance")
    gst = folders.create_personal(emp, "GST", finance.pk)
    assert gst.parent_id == finance.pk and gst.on_date is None and finance.on_date is not None
    assert [f.name for f in folders.ancestors(gst)] == ["Finance", "GST"]
    assert AuditLog.objects.filter(action="folder.created").count() == 2


def test_sibling_names_are_unique_ignoring_case_but_reusable_elsewhere(office):
    emp = office["emp"]
    a = folders.create_personal(emp, "Finance")
    with pytest.raises(Conflict):
        folders.create_personal(emp, "finance")
    other = folders.create_personal(emp, "Tax")
    folders.create_personal(emp, "GST", a.pk)
    folders.create_personal(emp, "GST", other.pk)  # same name, different parent
    # another person may use the same name at the root
    folders.create_personal(office["sub"], "Finance")


def test_bad_names_refused(office):
    for bad in ["", "   ", "a/b", "a\\b", "x" * 121, ".."]:
        with pytest.raises(ValidationFailed):
            folders.create_personal(office["emp"], bad)


def test_depth_is_capped(office):
    emp, parent = office["emp"], None
    for i in range(folders.MAX_DEPTH):
        parent = folders.create_personal(emp, f"L{i}", parent.pk if parent else None)
    with pytest.raises(ValidationFailed):
        folders.create_personal(emp, "too deep", parent.pk)


def test_nobody_else_can_name_or_open_my_folder(office):
    f = folders.create_personal(office["emp"], "Private")
    for fn in (
        lambda: folders.create_personal(office["sub"], "x", f.pk),
        lambda: folders.rename_personal(office["sub"], f.pk, "y"),
        lambda: folders.move_personal(office["sa"], f.pk, None),
        lambda: folders.my_folder(office["sub"], f.pk),
    ):
        with pytest.raises(NotFound):
            fn()
    with pytest.raises(NotFound):
        folders.my_folder(office["emp"], uuid.uuid4())


def test_hr_keeps_their_own_personal_folders_but_cannot_touch_anyone_elses(office):
    hr = make_user(Role.HR, reports_to=office["sa"], login_id="hrx")
    assert folders.create_personal(hr, "x").name == "x"
    other = folders.create_personal(office["emp"], "theirs")
    with pytest.raises(NotFound):
        folders.my_folder(hr, other.pk)


def test_rename_and_duplicate_rename(office):
    emp = office["emp"]
    a, b = folders.create_personal(emp, "A"), folders.create_personal(emp, "B")
    assert folders.rename_personal(emp, a.pk, "Alpha").name == "Alpha"
    with pytest.raises(Conflict):
        folders.rename_personal(emp, b.pk, "alpha")


def test_move_into_another_folder_and_back_to_a_root_but_never_into_itself(office):
    emp = office["emp"]
    a = folders.create_personal(emp, "A")
    b = folders.create_personal(emp, "B", a.pk)
    c = folders.create_personal(emp, "C", b.pk)
    with pytest.raises(ValidationFailed):
        folders.move_personal(emp, a.pk, c.pk)  # into its own grandchild
    with pytest.raises(ValidationFailed):
        folders.move_personal(emp, a.pk, a.pk)
    moved = folders.move_personal(emp, c.pk, None)
    assert moved.parent_id is None and moved.on_date is not None
    assert AuditLog.objects.filter(action="folder.moved").count() == 1


def test_move_respects_the_depth_cap_for_the_whole_branch(office):
    emp = office["emp"]
    chain, parent = [], None
    for i in range(folders.MAX_DEPTH - 1):
        parent = folders.create_personal(emp, f"D{i}", parent.pk if parent else None)
    branch = folders.create_personal(emp, "Branch")
    folders.create_personal(emp, "Leaf", branch.pk)  # branch is 2 deep
    with pytest.raises(ValidationFailed):
        folders.move_personal(emp, branch.pk, parent.pk)
    assert chain == []


def test_ensure_path_builds_and_reuses_a_tree(office):
    emp = office["emp"]
    leaf = folders.ensure_path(emp, ["Client", "2026", "Returns"])
    again = folders.ensure_path(emp, ["Client", "2026", "Returns"])
    assert leaf.pk == again.pk
    assert Folder.objects.filter(owner=emp).count() == 3


def test_place_a_file_in_a_folder_and_back(office):
    emp = office["emp"]
    v = mine_upload("emp")
    fol = folders.create_personal(emp, "Docs")
    assert folders.place_personal_file(emp, v.file_id, fol.pk).folder_id == fol.pk
    assert folders.place_personal_file(emp, v.file_id, None).folder_id is None
    with pytest.raises(NotFound):
        folders.place_personal_file(office["sub"], v.file_id, None)
    other = folders.create_personal(office["sub"], "Theirs")
    with pytest.raises(NotFound):
        folders.place_personal_file(emp, v.file_id, other.pk)  # someone else's folder
    assert File.objects.get(pk=v.file_id).visibility == FileVisibility.PRIVATE


def test_a_root_folder_stays_under_the_date_it_was_made(office):
    day = datetime.date(2026, 11, 1)
    f = folders.create_personal(office["emp"], "Old", on_date=day)
    assert folders.root_date(f) == day
