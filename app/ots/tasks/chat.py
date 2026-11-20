"""The task chat (IMP-15.8): one time-ordered conversation made from what already exists. Messages are the
task's comments (text, voice, help-request messages); files are the task's file links; system lines come from
the task's events, so they cannot disagree with what happened. Nothing is stored twice."""

from __future__ import annotations

from typing import Any

from ots.files import taskstorage
from ots.files.models import FileStatus

from .models import SupportRequest, Task, TaskComment, TaskEvent

EVENT_LINES = {
    "assignment.started": "started working",
    "assignment.submitted": "submitted the task for review",
    "assignment.approved": "approved the task",
    "assignment.changes_requested": "asked for changes",
    "assignment.blocked": "is waiting",
    "assignment.unblocked": "is back at work",
    "task.cancelled": "cancelled the task",
}


def side_of(author_id: Any, task: Task, assignee_ids: set[Any]) -> str:
    """The manager side is on the left, the people doing the work on the right, whoever is looking."""
    return "right" if author_id in assignee_ids and author_id != task.created_by_id else "left"


def build(
    viewer: Any, task: Task, comments: list[TaskComment], assignee_ids: set[Any]
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    ids = {c.help_request_id for c in comments if c.help_request_id}
    requests = {r.pk: r for r in SupportRequest.objects.filter(pk__in=ids).select_related("raised_by")}
    first_of: dict[Any, Any] = {}
    for c in comments:  # comments arrive oldest first: the first message of a request is its card
        if c.help_request_id and c.help_request_id not in first_of:
            first_of[c.help_request_id] = c.pk
    for c in comments:
        sr = requests.get(c.help_request_id) if c.help_request_id else None
        kind = "voice" if c.kind == "VOICE" else "request" if sr and first_of.get(sr.pk) == c.pk else "text"
        out.append(
            {
                "at": c.created_at, "kind": kind, "side": side_of(c.author_id, task, assignee_ids),
                "mid": f"c-{c.pk}", "sig": sr.status if sr and kind == "request" else "",
                "mine": c.author_id == viewer.pk,
                "who": c.author.full_name, "comment": c, "body": c.body, "request": sr,
                "reply_to_request": bool(sr) and kind == "text",
            }
        )  # fmt: skip
    events = TaskEvent.objects.filter(task=task, event_type__in=list(EVENT_LINES)).select_related("actor")
    for e in events.order_by("created_at")[:300]:
        who = e.actor.full_name if e.actor else "The system"
        out.append(
            {
                "at": e.created_at, "kind": "system", "side": "center", "mid": f"s-{e.pk}", "sig": "",
                "who": who, "body": f"{who} {EVENT_LINES[e.event_type]}", "note": taskstorage._event_note(e),
            }
        )  # fmt: skip
    links = (
        taskstorage._visible_links(viewer, task)
        .select_related("file", "version", "version__uploaded_by")
        .order_by("version__uploaded_at")[:300]
    )
    for link in links:
        v = link.version
        if v.file.status != FileStatus.ACTIVE:
            continue
        out.append(
            {
                "at": v.uploaded_at, "kind": "file", "side": side_of(v.uploaded_by_id, task, assignee_ids),
                "mid": f"f-{v.pk}", "sig": "", "mine": v.uploaded_by_id == viewer.pk,
                "who": v.uploaded_by.full_name, "version": v, "file": v.file, "note": v.note,
            }
        )  # fmt: skip
    out.sort(key=lambda m: m["at"])
    prev: dict[str, Any] | None = None
    for (
        m
    ) in out:  # one person's messages close together read as one run: they sit closer and the name shows once
        same = bool(prev) and prev["who"] == m["who"] and "system" not in (prev["kind"], m["kind"])
        m["cont"] = same and (m["at"] - prev["at"]).total_seconds() <= 300  # type: ignore[index]
        prev = m
    return out
