"""M10.3: the bell can refresh on its own, new notifications are announced, and the page carries the live scripts."""

import pytest
from django.test import Client
from ots.accounts.models import Role
from ots.live import publisher
from ots.notifications import services
from ots.notifications.models import Kind

from tests.accounts.factories import make_user
from tests.tasks.factories import make_task

pytestmark = pytest.mark.django_db
PW = "correct horse battery"


def login(login_id):
    c = Client()
    assert c.post("/login", {"login_id": login_id, "password": PW}).status_code == 302
    return c


def test_the_bell_fragment_shows_the_unread_count_and_needs_a_login():
    boss = make_user(Role.SUPER_ADMIN, login_id="boss")
    sub = make_user(Role.SUB_ADMIN, reports_to=boss, login_id="sub")
    emp = make_user(Role.EMPLOYEE, reports_to=sub, login_id="emp")
    task = make_task(sub, emp)
    services.notify([emp.pk], Kind.CHANGED, task, "Changed", actor=sub)
    html = login("emp").get("/app/notifications/badge").content.decode()
    assert 'id="ots-bell"' in html and "unread" in html and 'class="ots-count"' in html
    assert Client().get("/app/notifications/badge").status_code == 302


def test_every_page_has_the_bell_and_the_live_scripts():
    make_user(Role.SUPER_ADMIN, login_id="boss")
    page = login("boss").get("/app/tasks").content.decode()
    assert 'id="ots-bell"' in page and "js/live-core.js" in page and "js/live.js" in page
    assert page.index("js/live-core.js") < page.index("js/live.js")  # the rules load before the wiring


def test_new_notifications_are_announced_to_exactly_their_people(monkeypatch):
    sent = []
    monkeypatch.setattr(publisher, "send", lambda **kw: sent.append(kw))
    boss = make_user(Role.SUPER_ADMIN, login_id="boss")
    sub = make_user(Role.SUB_ADMIN, reports_to=boss, login_id="sub")
    emp = make_user(Role.EMPLOYEE, reports_to=sub, login_id="emp")
    task = make_task(sub, emp)
    services.notify([emp.pk, sub.pk], Kind.CHANGED, task, "Changed", actor=sub)  # not to the actor
    assert len(sent) == 1 and sent[0]["type_"] == "notification" and list(sent[0]["users"]) == [emp.pk]
    services.notify([sub.pk], Kind.CHANGED, task, "Changed", actor=sub)  # nobody to tell
    assert len(sent) == 1
