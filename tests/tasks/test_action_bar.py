"""IMP-17: one composer, one action bar. Every task action has exactly one home; sending a message never moves the
work; the voice card is gone; the shared menu rows cover the whole row."""

import re

import pytest
from django.test import Client
from ots.tasks import services
from ots.tasks.models import AssignmentStatus as S

from tests.tasks.test_workspace_chat import ask, login, started

pytestmark = pytest.mark.django_db


def page(who, task):
    return login(who).get(f"/app/tasks/{task.pk}").content.decode()


def bar_of(html):
    """The action bar region, with the People panel and everything after it cut away."""
    if 'aria-label="Task actions"' not in html:
        return ""
    a = html.index('aria-label="Task actions"')
    return html[a : html.index("</section>", a)]


def test_an_employee_in_progress_sees_waiting_and_submit_once_each(org, task):
    a = started(org, task)
    html = page("emp", task)
    assert html.count(f'data-assignment="{a.pk}"') == 1  # the one "I'm waiting" button
    assert html.count(f'data-open-dialog="act-{a.pk}-submit"') == 1
    assert html.count(f'id="act-{a.pk}-submit"') == 1
    assert html.count("Task actions") == 1


def test_remove_and_cancel_this_part_live_only_in_the_people_panel(org, task):
    a = started(org, task)
    html = page("mgr", task)
    for action in ("remove", "cancel"):
        key = f'data-open-dialog="act-{a.pk}-{action}"'
        assert html.count(key) == 1, action
        assert html.index(key) > html.index('aria-label="Task panel"')  # not in the bar, not in the composer
    assert "Remove from task" not in bar_of(html) and "Cancel this part" not in bar_of(html)


def test_the_bar_sits_between_the_header_and_the_chat_and_is_not_in_the_composer(org, task):
    started(org, task)
    html = page("emp", task)
    assert html.index('aria-label="Task"') < html.index("data-task-actions") < html.index('role="tablist"')
    composer = html[
        html.index("data-composer") : html.index('data-live-region="actions"')
        if html.index('data-live-region="actions"') > html.index("data-composer")
        else None
    ]
    for label in ("Submit task for review", "Approve task", "Request changes", "Back to work"):
        assert label not in composer, label


def test_submitted_work_shows_a_manager_approve_and_request_changes_once_and_the_employee_none(org, task):
    a = started(org, task)
    services.transition(a.pk, "submit", org["emp"], note="All figures are in the sheet.")
    mgr, emp = page("mgr", task), page("emp", task)
    assert mgr.count(f'action="/app/assignments/{a.pk}/approve"') == 1
    assert mgr.count(f'data-open-dialog="act-{a.pk}-request_changes"') == 1
    assert f"act-{a.pk}-submit" not in emp and "/approve" not in emp  # no second submit, no review buttons
    assert "Submitted" in emp or "review" in emp.lower()


def test_after_approval_no_review_buttons_remain(org, task):
    a = started(org, task)
    services.transition(a.pk, "submit", org["emp"], note="All figures are in the sheet.")
    services.transition(a.pk, "approve", org["mgr"])
    mgr = page("mgr", task)
    assert f"/assignments/{a.pk}/approve" not in mgr and f"act-{a.pk}-request_changes" not in mgr


def test_a_blocked_employee_gets_back_to_work_once(org, task):
    a = started(org, task)
    ask(org, a)
    html = page("emp", task)
    assert html.count(f'data-open-dialog="act-{a.pk}-unblock"') == 1
    assert f"act-{a.pk}-submit" not in html


def test_the_separate_voice_card_is_gone_and_the_mic_is_inside_the_composer(org, task):
    html = page("emp", task)
    assert 'aria-label="Voice message"' not in html and "Voice message up to" not in html
    assert html.count('aria-label="Record a voice message"') == 1
    composer = html[html.index("data-composer") :]
    assert composer.index('aria-label="Record a voice message"') < composer.index('aria-label="Send"')
    assert html.count('name="body" rows="1"') == 1  # one message box, not two


def test_sending_a_message_never_changes_the_work(org, task):
    a = started(org, task)
    c = login("emp")
    r = c.post(f"/app/tasks/{task.pk}/comment", {"body": "status update"}, HTTP_X_REQUESTED_WITH="ots-chat",
               HTTP_IDEMPOTENCY_KEY="0190a0a0-0000-7000-8000-000000000001")  # fmt: skip
    assert r.status_code == 200
    a.refresh_from_db()
    assert a.status == S.IN_PROGRESS


def test_the_profile_menu_rows_share_one_component_and_lunch_asks_first(org, task):
    html = page("emp", task)
    menu = html[html.index('role="menu"') : html.index("</header>")]
    assert menu.count('class="menu-item"') == 5
    lunch = re.search(r'<button type="button" class="menu-item"[^>]*data-open-dialog="lunch-confirm"', menu)
    assert lunch, "the lunch item opens a confirmation, it does not log out by itself"
    dialog = html[html.index('id="lunch-confirm"') :]
    assert 'name="reason" value="lunch"' in dialog and "data-close-dialog" in dialog


def test_the_attachment_menus_use_whole_row_items(org, task):
    html = page("emp", task)
    menu = html[html.index("menu-attach") : html.index("data-composer") + 6000]
    assert menu.count('class="menu-item"') >= 2 and "h-5 w-full" not in menu


def test_the_compiled_css_gives_the_whole_row_a_hover(org, task):
    css = open("app/static/css/app.css").read()
    for needle in (".menu-item:hover", ".menu-item:focus-visible", ".menu-item:active", ".menu-attach"):
        assert needle in css, needle
    assert Client().get("/login").status_code == 200
