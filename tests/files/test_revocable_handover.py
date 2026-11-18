"""IMP-15.6 (IMP-13.4, D4): every hand-over is revocable by its sender or the Super Admin; history is kept. And
the Super Admin's audited look at a person's personal storage."""

import pytest
from ots.accounts.models import Role
from ots.audit.models import AuditLog
from ots.core.errors import NotFound
from ots.files import handover, personal
from ots.files.models import FileAccessLog, FileHandoverRecipient

from tests.accounts.factories import make_user
from tests.files.conftest import login
from tests.files.test_personal import mine_upload

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.usefixtures("dirs")]


def shared(office):
    v = mine_upload("sub", name="2026-11-17_Sheet.txt")
    h = handover.send(office["sub"], v.pk, [office["emp"].pk], "please check")
    return v, FileHandoverRecipient.objects.get(handover=h)


def test_revoking_closes_download_preview_and_the_shared_list_at_once(office):
    v, row = shared(office)
    e = login("emp")
    assert e.get(f"/files/v/{v.pk}/download").status_code in (200, 503)
    assert "Sheet" in e.get("/files/shared").content.decode()
    r = login("sub").post(f"/files/sharing/{row.pk}/stop")
    assert r.status_code == 302
    row.refresh_from_db()
    assert row.revoked_at is not None and row.revoked_by_id == office["sub"].pk
    for url in (f"/files/v/{v.pk}/download", f"/files/v/{v.pk}/preview"):
        assert e.get(url).status_code == 404
    assert "Sheet" not in e.get("/files/shared").content.decode()
    assert v.pk not in [x.pk for x in personal.shared_with_me(office["emp"])]
    assert AuditLog.objects.filter(action="file.handover_revoked").count() == 1


def test_the_row_and_its_history_stay_and_the_sender_still_sees_who_had_it(office):
    v, row = shared(office)
    login("sub").post(f"/files/sharing/{row.pk}/stop")
    page = login("sub").get(f"/files/v/{v.pk}/sharing").content.decode()
    assert office["emp"].full_name in page and "Sharing stopped" in page and ">Stop sharing<" not in page
    assert FileHandoverRecipient.objects.filter(pk=row.pk).exists()


def test_only_the_sender_or_the_super_admin_may_stop_it(office):
    v, row = shared(office)
    for who in ("emp", "other"):
        assert login(who).post(f"/files/sharing/{row.pk}/stop").status_code == 404
    row.refresh_from_db()
    assert row.revoked_at is None
    assert login("boss").post(f"/files/sharing/{row.pk}/stop").status_code == 302  # the Super Admin
    row.refresh_from_db()
    assert row.revoked_at is not None and row.revoked_by_id == office["sa"].pk


def test_a_second_stop_is_harmless_and_the_sharing_page_is_private(office):
    v, row = shared(office)
    c = login("sub")
    c.post(f"/files/sharing/{row.pk}/stop")
    first = FileHandoverRecipient.objects.get(pk=row.pk).revoked_at
    c.post(f"/files/sharing/{row.pk}/stop")
    assert FileHandoverRecipient.objects.get(pk=row.pk).revoked_at == first
    assert (
        login("emp").get(f"/files/v/{v.pk}/sharing").status_code == 404
    )  # the recipient cannot list recipients
    assert login("other").get(f"/files/v/{v.pk}/sharing").status_code == 404
    with pytest.raises(NotFound):
        handover.revoke(office["emp"], row.pk)


def test_sharing_again_after_stopping_works(office):
    v, row = shared(office)
    login("sub").post(f"/files/sharing/{row.pk}/stop")
    handover.send(office["sub"], v.pk, [office["emp"].pk], "second try")
    assert "Sheet" in login("emp").get("/files/shared").content.decode()


def test_the_super_admin_browses_a_persons_storage_and_it_is_recorded(office):
    v = mine_upload("emp", name="2026-11-17_Private-notes.txt")
    c = login("boss")
    assert (
        "Asha Kulkarni" in c.get("/files/people").content.decode()
        or office["emp"].full_name in c.get("/files/people").content.decode()
    )
    page = c.get(f"/files/people/{office['emp'].pk}").content.decode()
    assert "Private-notes" in page and "recorded" in page
    assert AuditLog.objects.filter(action="storage.browsed").count() == 1
    assert c.get(f"/files/v/{v.pk}/download").status_code in (200, 503)
    assert FileAccessLog.objects.filter(version=v, user=office["sa"]).exists()


def test_nobody_else_can_browse_personal_storage_not_even_hr_or_the_manager(office):
    mine_upload("emp", name="2026-11-17_Private-notes.txt")
    make_user(Role.HR, reports_to=office["sa"], login_id="hrb")
    for who in ("sub", "emp", "other", "hrb"):
        assert login(who).get("/files/people").status_code in (403, 404), who
        assert login(who).get(f"/files/people/{office['emp'].pk}").status_code in (403, 404), who
    assert not AuditLog.objects.filter(action="storage.browsed").exists()
    assert "People&#x27;s storage" not in login("sub").get("/files/mine").content.decode()
    assert "People&#x27;s storage" in login("boss").get("/files/mine").content.decode()
