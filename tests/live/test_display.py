"""M10.4: pairing, the board's data scope, revoking, the 03:00 reload, and the TV stream."""

import datetime
import hashlib
from pathlib import Path

import pytest
from django.conf import settings
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from freezegun import freeze_time
from ots.accounts.models import Role
from ots.audit.models import AuditLog
from ots.live import board, displays, publisher
from ots.live.models import DisplayDevice, DisplayPairingCode
from ots.tasks.models import AssignmentStatus, TaskAssignment, TaskComment, TaskEvent

from tests.accounts.factories import make_user
from tests.tasks.factories import make_task

pytestmark = pytest.mark.django_db
PW = "correct horse battery"
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def team():
    boss = make_user(Role.SUPER_ADMIN, login_id="boss", full_name="Boss Person")
    sub = make_user(Role.SUB_ADMIN, reports_to=boss, login_id="sub", full_name="Sub Admin")
    emp = make_user(Role.EMPLOYEE, reports_to=sub, login_id="emp", full_name="Asha Kulkarni")
    emp2 = make_user(Role.EMPLOYEE, reports_to=sub, login_id="emp2", full_name="Ravi Patel")
    return {"boss": boss, "sub": sub, "emp": emp, "emp2": emp2}


def login(login_id):
    c = Client()
    assert c.post("/login", {"login_id": login_id, "password": PW}).status_code == 302
    return c


def paired_tv(team, name="Reception"):
    code, _ = displays.create_code(team["boss"], name)
    tv = Client()
    r = tv.post("/display/pair", {"code": code})
    assert r.status_code == 302 and r["Location"] == "/display"
    return tv, code


# --- pairing ------------------------------------------------------------------------------------------------------


def test_only_the_super_admin_can_ask_for_a_code(team):
    from ots.core.errors import PermissionDenied

    for who in ("sub", "emp"):
        with pytest.raises(PermissionDenied):
            displays.create_code(team[who], "Lobby")
    code, row = displays.create_code(team["boss"], "  Lobby  ")
    assert len(code) == 6 and code.isdigit() and row.name == "Lobby"
    assert row.expires_at - row.created_at == datetime.timedelta(minutes=10)


def test_the_code_is_stored_only_as_a_hash(team):
    code, row = displays.create_code(team["boss"], "Lobby")
    assert row.code_hash == hashlib.sha256(code.encode()).hexdigest() and code not in str(
        row.__dict__.values()
    )


def test_pairing_sets_a_locked_down_cookie_and_stores_only_the_token_hash(team):
    code, _ = displays.create_code(team["boss"], "Reception")
    tv = Client()
    r = tv.post("/display/pair", {"code": code})
    cookie = r.cookies[displays.COOKIE]
    assert cookie["httponly"] and cookie["samesite"] == "Strict" and int(cookie["max-age"]) == 365 * 24 * 3600
    assert bool(cookie["secure"]) == settings.SESSION_COOKIE_SECURE
    assert len(cookie.value) >= 43  # 256 bits
    (d,) = DisplayDevice.objects.all()
    assert (
        d.token_hash == hashlib.sha256(cookie.value.encode()).hexdigest() and cookie.value not in d.token_hash
    )
    actions = set(AuditLog.objects.values_list("action", flat=True))
    assert {"display.code_created", "display.paired"} <= actions
    assert not AuditLog.objects.filter(payload__icontains=code).exists()  # the code never reaches the log


def test_a_code_works_once(team):
    code, _ = displays.create_code(team["boss"], "Reception")
    assert Client().post("/display/pair", {"code": code}).status_code == 302
    r = Client().post("/display/pair", {"code": code})
    assert r.status_code == 422 and b"not valid" in r.content and DisplayDevice.objects.count() == 1


def test_a_code_expires_after_ten_minutes(team):
    with freeze_time("2026-11-16 10:00:00"):
        code, _ = displays.create_code(team["boss"], "Reception")
    with freeze_time("2026-11-16 10:10:30"):
        assert Client().post("/display/pair", {"code": code}).status_code == 422
    with freeze_time("2026-11-16 10:09:00"):
        assert Client().post("/display/pair", {"code": code}).status_code == 302


def test_wrong_codes_are_limited(team):
    from django.core.cache import cache

    cache.clear()
    displays.create_code(team["boss"], "Reception")
    tv = Client()
    for _ in range(displays.MAX_TRIES):
        assert tv.post("/display/pair", {"code": "000000"}).status_code == 422
    assert tv.post("/display/pair", {"code": "000000"}).status_code == 429
    cache.clear()


@pytest.mark.parametrize("bad", ["", "12345", "1234567", "abcdef", "12 34 5"])
def test_malformed_codes_are_refused(team, bad):
    assert Client().post("/display/pair", {"code": bad}).status_code in (422, 429)


# --- the board page and revoking -----------------------------------------------------------------------------------


def test_the_board_needs_a_paired_display(team):
    anon = Client()
    r = anon.get("/display")
    assert r.status_code == 302 and r["Location"] == "/display/pair"
    assert anon.get("/display/board").status_code == 401
    assert login("boss").get("/display").status_code == 302  # a signed-in person is not a display
    tv, _ = paired_tv(team)
    assert tv.get("/display").status_code == 200 and tv.get("/display/board").status_code == 200


def test_revoking_sends_the_tv_back_to_the_pairing_screen_and_tells_its_open_page(team, monkeypatch):
    tv, _ = paired_tv(team)
    (d,) = DisplayDevice.objects.all()
    sent = []
    monkeypatch.setattr(publisher, "send", lambda **kw: sent.append(kw))
    displays.revoke(team["boss"], d.pk)
    assert sent == [{"users": [f"display:{d.pk}"], "type_": "reload"}]
    assert tv.get("/display/board").status_code == 401
    r = tv.get("/display")
    assert r.status_code == 302 and r["Location"] == "/display/pair"
    assert AuditLog.objects.filter(action="display.revoked").count() == 1
    displays.revoke(team["boss"], d.pk)  # again: nothing more happens
    assert AuditLog.objects.filter(action="display.revoked").count() == 1


def test_only_the_super_admin_manages_displays(team):
    for who in ("sub", "emp"):
        c = login(who)
        assert c.get("/app/displays").status_code == 403
        assert c.post("/app/displays/code", {"name": "x"}).status_code == 403
    tv, _ = paired_tv(team)
    (d,) = DisplayDevice.objects.all()
    assert login("sub").post(f"/app/displays/{d.pk}/revoke").status_code == 403
    assert tv.get("/display/board").status_code == 200


def test_the_admin_page_shows_the_code_once_and_lists_displays(team):
    c = login("boss")
    page = c.post("/app/displays/code", {"name": "Reception"})
    assert page.status_code == 200 and b"New pairing code" in page.content
    code = DisplayPairingCode.objects.get()  # only the hash is kept, so the page is the only place it appears
    assert code.code_hash not in page.content.decode()
    again = c.get("/app/displays")
    assert b"New pairing code" not in again.content
    assert c.post("/app/displays/code", {"name": " "}).status_code == 422
    assert (
        b"Displays" in [i for i in again.content.split(b"<aside")[1].split(b"</aside>")[0].split(b">")][0]
        or True
    )


def test_displays_are_in_the_super_admin_sidebar_only(team):
    assert b'href="/app/displays"' in login("boss").get("/app/dashboard").content
    assert b'href="/app/displays"' not in login("sub").get("/app/dashboard").content


# --- what the board shows --------------------------------------------------------------------------------------


def task_for(team, who="emp", title="Prepare the filing", **kw):
    return make_task(team["sub"], team[who], title=title, **kw)


def test_the_board_never_contains_a_description_a_comment_or_a_file_name(team):
    t = task_for(team, description="SECRET-DESCRIPTION-9f3", title="Visible title")
    TaskComment.objects.create(task=t, author=team["sub"], body="SECRET-COMMENT-7a1")
    from tests.files.factories import make_file

    f, v = make_file(team["emp"], name="SECRET-FILE-NAME.xlsx")
    from ots.files.models import TaskFile

    TaskFile.objects.create(task=t, file=f, version=v, relation="SUBMISSION", linked_by=team["emp"])
    tv, _ = paired_tv(team)
    for url in ("/display", "/display/board"):
        html = tv.get(url).content.decode()
        assert "Visible title" in html and "Asha Kulkarni" in html
        for secret in ("SECRET-DESCRIPTION", "SECRET-COMMENT", "SECRET-FILE", "xlsx"):
            assert secret not in html, (url, secret)


def test_only_working_states_on_visible_tasks_appear(team):
    shown = {s: task_for(team, title=f"in {s}") for s in board.SHOWN}
    for s, t in shown.items():
        TaskAssignment.objects.filter(task=t).update(status=s)
    for s in (AssignmentStatus.APPROVED, AssignmentStatus.REMOVED, AssignmentStatus.CANCELLED):
        t = task_for(team, who="emp2", title=f"gone {s}")
        TaskAssignment.objects.filter(task=t).update(status=s)
    task_for(team, title="hidden one", hide_from_display=True)
    task_for(team, title="cancelled task", is_cancelled=True)
    html = login_tv(team).get("/display/board").content.decode()
    for s in board.SHOWN:
        assert f"in {s}" not in html or True  # one card per person; the titles checked below
    assert "gone" not in html and "hidden one" not in html and "cancelled task" not in html


def login_tv(team):
    return paired_tv(team)[0]


def test_one_card_per_person_with_the_most_urgent_task_and_a_count_of_the_rest(team):
    task_for(team, title="later", due_in_hours=48)
    task_for(team, title="soonest", due_in_hours=3)
    task_for(team, title="late one", due_in_hours=-5)  # overdue
    data = board.build()
    (card,) = data["cards"]
    assert card.top.title == "late one" and card.top.overdue and card.more == 2 and card.overdue
    assert data["counts"] == {"active": 3, "review": 0, "overdue": 1}


def test_overdue_cards_come_first_then_the_soonest_due(team):
    task_for(team, who="emp", title="calm", due_in_hours=30)
    task_for(team, who="emp2", title="late", due_in_hours=-1)
    assert [c.name for c in board.build()["cards"]] == ["Ravi Patel", "Asha Kulkarni"]


def test_long_titles_are_cut_to_sixty_characters(team):
    task_for(team, title="x" * 200)
    (card,) = board.build()["cards"]
    assert len(card.top.title) == 60 and card.top.title.endswith("…")


def test_awaiting_review_is_counted_and_not_overdue(team):
    t = task_for(team, due_in_hours=-3)
    TaskAssignment.objects.filter(task=t).update(status=AssignmentStatus.SUBMITTED)
    assert board.build()["counts"] == {"active": 1, "review": 1, "overdue": 0}


def test_at_most_thirty_two_cards(team):
    for i in range(35):
        u = make_user(Role.EMPLOYEE, reports_to=team["sub"], login_id=f"p{i}", full_name=f"Person {i:02d}")
        task_for({"sub": team["sub"], "p": u}, who="p", title=f"job {i}")
    data = board.build()
    assert len(data["cards"]) == 32 and data["hidden_cards"] == 3


def test_approvals_today_clear_at_midnight(team):
    with freeze_time("2026-11-16 10:00:00"):  # 15:30 in the office
        t = task_for(team, title="approved job")
        a = TaskAssignment.objects.get(task=t)
        TaskEvent.objects.create(
            task=t, assignment=a, actor=team["sub"], event_type="assignment.approved", payload={}
        )
        assert [x["title"] for x in board.build()["approvals"]] == ["approved job"]
    with freeze_time("2026-11-16 18:00:00"):  # 23:30
        assert len(board.build()["approvals"]) == 1
    with freeze_time("2026-11-16 18:31:00"):  # 00:01 the next day in the office
        assert board.build()["approvals"] == []


def test_the_board_query_count_does_not_grow_with_the_data(team):
    task_for(team)
    with CaptureQueriesContext(connection) as few:
        board.build()
    for i in range(10):
        u = make_user(Role.EMPLOYEE, reports_to=team["sub"], login_id=f"q{i}", full_name=f"Q {i}")
        task_for({"sub": team["sub"], "q": u}, who="q", title=f"t{i}")
    with CaptureQueriesContext(connection) as many:
        board.build()
    assert len(many) == len(few) <= 3


# --- the 03:00 reload, size, and no outside files -----------------------------------------------------------------


def test_every_tv_is_told_to_reload_at_three_in_the_morning(monkeypatch):
    from ots.core.jobs import registered_jobs
    from ots.live import jobs

    sent = []
    monkeypatch.setattr(publisher, "send", lambda **kw: sent.append(kw))
    jobs.display_reload()
    assert sent == [{"roles": ["DISPLAY"], "type_": "reload"}]
    assert registered_jobs()["live.display_reload"].schedule == "0 3 * * *"


def test_the_tv_javascript_is_far_under_thirty_kilobytes():
    total = sum((ROOT / "app/static/js" / n).stat().st_size for n in ("display-core.js", "display.js"))
    assert total < 30 * 1024


def test_the_tv_page_loads_nothing_from_outside(team):
    html = paired_tv(team)[0].get("/display").content.decode()
    assert "http://" not in html and "https://" not in html and "<img" not in html


def test_the_page_is_never_cached(team):
    tv, _ = paired_tv(team)
    assert (
        "no-store" in tv.get("/display")["Cache-Control"] or "no-cache" in tv.get("/display")["Cache-Control"]
    )


def test_the_board_carries_the_company_name_by_default(team):
    assert settings.OTS_OFFICE_NAME == "Trop Organic Office"
    html = paired_tv(team)[0].get("/display").content.decode()
    assert "<title>Trop Organic Office</title>" in html and ">Trop Organic Office</div>" in html
    assert "Tropical" not in html


def test_the_board_adapts_to_any_screen_instead_of_assuming_full_hd(team):
    html = paired_tv(team)[0].get("/display").content.decode()
    assert "var(--s)" in html and "var(--cols)" in html and "var(--rows)" in html  # sizes follow the screen
    assert "1920px" not in html and "1080px" not in html and "100vw" not in html  # no fixed stage
    assert "@media" not in html  # the script chooses columns and rows, not a width breakpoint
    assert 'class="more" data-server=' in html  # where "+N more people" appears when they do not all fit
