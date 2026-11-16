"""M10.1: which live message each business event becomes, and who gets it (ids only, never text)."""

import json

import pytest
from ots.accounts.models import Role
from ots.core.clock import now
from ots.core.events import Event
from ots.live import publisher

from tests.accounts.factories import make_user
from tests.tasks.factories import make_task

pytestmark = pytest.mark.django_db


class _Notify:
    """Stands in for the database connection inside publisher.send: records each pg_notify payload."""

    def __init__(self):
        self.seen = []

    def cursor(self):
        outer = self

        class Cur:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql, params):
                outer.seen.append(params[1])

        return Cur()


@pytest.fixture
def sent(monkeypatch):
    calls = []
    monkeypatch.setattr(publisher, "send", lambda **kw: calls.append(kw))
    return calls


@pytest.fixture
def team():
    boss = make_user(Role.SUPER_ADMIN, login_id="boss")
    sub = make_user(Role.SUB_ADMIN, reports_to=boss, login_id="sub")
    emp = make_user(Role.EMPLOYEE, reports_to=sub, login_id="emp")
    stranger = make_user(Role.EMPLOYEE, reports_to=boss, login_id="stranger")
    task = make_task(sub, emp, title="Secret title")
    return {"boss": boss, "sub": sub, "emp": emp, "stranger": stranger, "task": task}


def ev(type_, payload, agg=("assignment", "x")):
    return Event(id="1", type=type_, aggregate_type=agg[0], aggregate_id=agg[1], payload=payload, at=now())


def test_a_task_change_reaches_the_creator_the_assignee_their_managers_and_the_super_admin_not_a_stranger(
    sent, team
):
    t = team["task"]
    publisher._task_changed(
        ev("assignment.started", {"task_id": str(t.pk), "assignee_id": str(team["emp"].pk)})
    )
    live, board = sent
    assert live["type_"] == "task-changed" and live["task_id"] == str(t.pk)
    assert set(live["users"]) == {str(team[k].pk) for k in ("emp", "sub", "boss")}
    assert str(team["stranger"].pk) not in live["users"] and "SUPER_ADMIN" in live["roles"]
    assert board == {"roles": ["DISPLAY"], "type_": "board-changed"}


def test_no_text_from_the_task_goes_over_the_wire(team, monkeypatch):
    fake = _Notify()
    monkeypatch.setattr(publisher, "connection", fake)
    publisher._task_changed(
        ev(
            "task.updated",
            {"task_id": str(team["task"].pk), "title": "Secret title", "description": "Secret body"},
        )
    )
    assert fake.seen and all("Secret" not in s for s in fake.seen)
    assert all(len(s.encode()) <= publisher.MAX_PAYLOAD for s in fake.seen)
    assert json.loads(fake.seen[0])["type"] == "task-changed"


def test_events_without_a_task_are_ignored(sent):
    publisher._task_changed(ev("task.updated", {}))
    assert sent == []


def test_a_file_going_off_site_tells_the_uploader_their_managers_and_the_task_people(sent, team):
    from tests.files.factories import make_file

    _, v = make_file(team["emp"])
    from ots.files.models import TaskFile

    TaskFile.objects.create(
        task=team["task"], file=v.file, version=v, relation="SUBMISSION", linked_by=team["emp"]
    )
    publisher._file_state(ev("file.archived", {}, ("file_version", str(v.pk))))
    (m,) = sent
    assert m["type_"] == "file-archived" and m["version_id"] == v.pk
    assert {str(team[k].pk) for k in ("emp", "sub", "boss")} <= set(m["users"])
    assert str(team["stranger"].pk) not in m["users"]


def test_an_alert_goes_to_super_admins_only(sent):
    publisher._alert(ev("alert.raised", {"key": "k", "severity": "WARNING"}, ("alert", "k")))
    assert sent == [{"roles": ["SUPER_ADMIN"], "type_": "alert", "key": "k", "severity": "WARNING"}]


def test_the_chain_of_managers_stops_at_the_top(team):
    assert publisher._chain([team["emp"].pk]) == {str(team[k].pk) for k in ("emp", "sub", "boss")}
    assert publisher._chain([]) == set()


def test_an_oversized_message_becomes_a_reload_for_super_admins(monkeypatch):
    fake = _Notify()
    monkeypatch.setattr(publisher, "connection", fake)
    publisher.send(users=[f"{i:036d}" for i in range(400)], type_="task-changed")
    assert [json.loads(x) for x in fake.seen] == [{"users": [], "roles": ["SUPER_ADMIN"], "type": "reload"}]
