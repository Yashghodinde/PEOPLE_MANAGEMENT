"""The worker side: turn an event into a message on the live channel. Ids only; the page re-fetches itself."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from django.db import connection

from ots.core.events import Event, subscribe

from .hub import CHANNEL

MAX_PAYLOAD = 7000  # PostgreSQL allows 8000 bytes; keep clear of it


def send(users: Iterable[Any] = (), roles: Iterable[str] = (), type_: str = "reload", **data: Any) -> None:
    body = {"users": sorted({str(u) for u in users}), "roles": sorted(roles), "type": type_}
    body.update({k: str(v) for k, v in data.items()})
    payload = json.dumps(body)
    if len(payload.encode()) > MAX_PAYLOAD:
        payload = json.dumps({"users": [], "roles": ["SUPER_ADMIN"], "type": "reload"})
    with connection.cursor() as cur:
        cur.execute("SELECT pg_notify(%s, %s)", [CHANNEL, payload])


@subscribe("session.ended")
def session_ended(event: Event) -> None:
    """The server closed a session (idle, browser closed, forced): the open page shows the logged-out page."""
    send(users=[event.payload["user_id"]], type_="logged-out", reason=event.payload.get("reason", ""))


# --- business events become live messages (M10 section 1): ids only, pages re-fetch what they show ---


def _chain(user_ids: Iterable[Any]) -> set[str]:
    """These people and everyone above them (their manager, that manager's manager, up to the top)."""
    from ots.accounts.models import User

    seen: set[str] = set()
    frontier = {str(u) for u in user_ids if u}
    while frontier - seen:
        seen |= frontier
        frontier = {
            str(m)
            for m in User.objects.filter(pk__in=frontier).values_list("reports_to_id", flat=True)
            if m is not None
        }
    return seen


def task_audience(task_id: Any, extra: Iterable[Any] = ()) -> set[str]:
    """Who has this task on screen: its creator, every assignee, and each of their managers."""
    from ots.tasks.models import Task, TaskAssignment

    people: set[Any] = set(extra)
    creator = Task.objects.filter(pk=task_id).values_list("created_by_id", flat=True).first()
    if creator:
        people.add(creator)
    people |= set(TaskAssignment.objects.filter(task_id=task_id).values_list("user_id", flat=True))
    return _chain(people)


def _task_changed(event: Event) -> None:
    p = event.payload
    task_id = p.get("task_id")
    if not task_id:
        return
    extra = [p.get(k) for k in ("assignee_id", "creator_id", "actor_id")]
    data = {"task_id": task_id}
    if p.get("assignment_id"):
        data["assignment_id"] = p["assignment_id"]
    send(users=task_audience(task_id, extra), roles=["SUPER_ADMIN"], type_="task-changed", **data)
    send(roles=["DISPLAY"], type_="board-changed")


for _name in (
    "task.created", "task.updated", "task.cancelled", "task.deadline_changed", "comment.added",
    "comment.voice_added",
    "assignment.created", "assignment.started", "assignment.paused", "assignment.resumed",
    "assignment.blocked", "assignment.unblocked", "assignment.submitted", "assignment.changes_requested",
    "assignment.approved", "assignment.removed", "assignment.cancelled",
    "file.version_added", "file.attached", "file.handed_over",
):  # fmt: skip
    subscribe(_name)(_task_changed)


@subscribe("file.archived")
@subscribe("file.missing")
@subscribe("file.drifted")
@subscribe("file.repaired")
def _file_state(event: Event) -> None:
    """A file's off-site badge changed: tell the uploader, their managers and the task's people."""
    from ots.files.models import FileVersion, TaskFile

    version = FileVersion.objects.filter(pk=event.aggregate_id).values("pk", "uploaded_by_id").first()
    if version is None:
        return
    people: set[Any] = {version["uploaded_by_id"]}
    for task_id in TaskFile.objects.filter(version_id=version["pk"]).values_list("task_id", flat=True):
        people |= task_audience(task_id)
    send(users=_chain(people), roles=["SUPER_ADMIN"], type_="file-archived", version_id=version["pk"])


@subscribe("file.handover_opened")
def _handover_opened(event: Event) -> None:
    from ots.files.models import FileHandover

    handover_id = event.payload.get("handover_id")
    h = FileHandover.objects.filter(pk=handover_id).values("from_user_id").first() if handover_id else None
    if h:
        send(users=[h["from_user_id"]], type_="file-archived", version_id=event.aggregate_id)


@subscribe("alert.raised")
def _alert(event: Event) -> None:
    p = event.payload
    send(roles=["SUPER_ADMIN"], type_="alert", key=p.get("key", ""), severity=p.get("severity", ""))


@subscribe("user.manager_changed")
@subscribe("user.deactivated")
def _people_changed(event: Event) -> None:
    send(roles=["SUPER_ADMIN"], type_="reload")
