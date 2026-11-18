"""Task storage (IMP-03): task files grouped by date, then task, then document, and the exchange timeline.

Nothing here is stored. A "document" is one `File`, its versions are `FileVersion` rows, and the words on each
row ("Shared", "Submitted", "Corrections", "Revised", "Reference") are worked out when the page is built.
Everything starts from `versions_visible_to`, so a person sees exactly the files they could open anyway.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Any

from django.db.models import Q

from ots.accounts.models import User
from ots.tasks.models import Task, TaskAssignment, TaskEvent

from .models import FileHandover, FileRelation, FileStatus, FileVersion, TaskFile
from .selectors import versions_visible_to

DAYS_PER_PAGE = 7
EVENT_WORDS = {
    "assignment.submitted": "submitted their work",
    "assignment.changes_requested": "asked for changes",
    "assignment.approved": "approved the work",
    "task.cancelled": "cancelled the task",
}


@dataclass
class Row:
    """One version of one document on one task."""

    version: FileVersion
    link: TaskFile
    action: str
    uploader_label: str
    differs: bool  # the shown name is not the original file name


@dataclass
class Document:
    file: Any
    rows: list[Row] = field(default_factory=list)


@dataclass
class TaskBlock:
    task: Task
    documents: list[Document] = field(default_factory=list)


@dataclass
class PersonBlock:
    """Team Storage (IMP-13.3): one employee's tasks on one day, for a manager or the boss."""

    name: str
    tasks: list[TaskBlock] = field(default_factory=list)


@dataclass
class DayBlock:
    day: datetime.date
    tasks: list[TaskBlock] = field(default_factory=list)  # an employee's own view: date, task, document
    people: list[PersonBlock] = field(
        default_factory=list
    )  # a manager's view: date, employee, task, document


def action_label(version: FileVersion, link: TaskFile, task: Task, assignees: set[Any]) -> str:
    """Shared / Submitted / Corrections / Revised / Reference. Computed, never stored."""
    if link.relation == FileRelation.REFERENCE:
        return "Reference"
    by_creator = version.uploaded_by_id == task.created_by_id
    by_assignee = version.uploaded_by_id in assignees and not by_creator
    if version.version_number == 1:
        return "Shared" if by_creator else "Submitted" if by_assignee else "Shared"
    return "Corrections" if by_creator else "Revised" if by_assignee else "Revised"


def _who(user: User) -> str:
    return f"{user.get_role_display()}" + (f", {user.designation}" if user.designation else "")


def _visible_links(viewer: Any, task: Task | None = None) -> Any:
    visible = versions_visible_to(viewer).filter(is_voice=False, file__status=FileStatus.ACTIVE).values("pk")
    qs = TaskFile.objects.filter(version__in=visible)
    return qs.filter(task=task) if task is not None else qs


def _employee_of(link: TaskFile, assignees: set[Any]) -> str:
    """Whose exchange a file belongs to: the assignee on its assignment, else the uploader if they work on
    the task, else the task's own people ("Everyone on the task" for a creator's reference sheet)."""
    if link.assignment_id and link.assignment.user_id:
        return str(link.assignment.user.full_name)
    if link.version.uploaded_by_id in assignees:
        return str(link.version.uploaded_by.full_name)
    return "Everyone on the task"


def task_storage(
    viewer: Any, before: datetime.date | None = None
) -> tuple[list[DayBlock], datetime.date | None]:
    """Days newest first, DAYS_PER_PAGE at a time; inside a day the tasks, inside a task the documents.

    A task whose files arrived on several days appears under each day; every row opens the same task and
    file (nothing is copied). Returns (days, the date to pass as `before` for the next page, or None).
    """
    from ots.accounts.models import Role

    team = viewer.role in (Role.SUB_ADMIN, Role.SUPER_ADMIN)
    links = _visible_links(viewer)
    if before is not None:
        links = links.filter(version__uploaded_on__lt=before)
    day_list = list(
        links.order_by()
        .values_list("version__uploaded_on", flat=True)
        .distinct()
        .order_by("-version__uploaded_on")[: DAYS_PER_PAGE + 1]
    )
    more = len(day_list) > DAYS_PER_PAGE
    day_list = day_list[:DAYS_PER_PAGE]
    if not day_list:
        return [], None
    rows = list(
        links.filter(version__uploaded_on__in=day_list)
        .select_related(
            "task", "file", "version", "version__uploaded_by", "version__blob", "assignment__user"
        )
        .order_by("version__uploaded_on", "task__task_code", "file__created_at", "version__version_number")
    )
    task_ids = {r.task_id for r in rows}
    assignees: dict[Any, set[Any]] = {}
    for tid, uid in TaskAssignment.objects.filter(task_id__in=task_ids).values_list("task_id", "user_id"):
        assignees.setdefault(tid, set()).add(uid)
    days: dict[datetime.date, DayBlock] = {}
    for r in rows:
        day = days.setdefault(r.version.uploaded_on, DayBlock(r.version.uploaded_on))
        holder = day.tasks
        if team:
            who = _employee_of(r, assignees.get(r.task_id, set()))
            pb = next((p for p in day.people if p.name == who), None)
            if pb is None:
                pb = PersonBlock(who)
                day.people.append(pb)
            holder = pb.tasks
        tb = next((t for t in holder if t.task.pk == r.task_id), None)
        if tb is None:
            tb = TaskBlock(r.task)
            holder.append(tb)
        doc = next((d for d in tb.documents if d.file.pk == r.file_id), None)
        if doc is None:
            doc = Document(r.file)
            tb.documents.append(doc)
        v = r.version
        doc.rows.append(
            Row(
                v,
                r,
                action_label(v, r, r.task, assignees.get(r.task_id, set())),
                _who(v.uploaded_by),
                bool(v.display_name) and v.display_name != v.original_filename,
            )  # fmt: skip
        )
    ordered = [days[d] for d in sorted(days, reverse=True)]
    for d in ordered:
        d.people.sort(key=lambda p: (p.name == "Everyone on the task", p.name.lower()))
    return ordered, (day_list[-1] if more else None)


# ---------------------------------------------------------------- the exchange timeline


def _event_note(e: TaskEvent) -> str:
    """The words the person wrote with the action: the feedback, the closing note or the reason."""
    given = (e.payload or {}).get("inputs") or {}
    return str(given.get("feedback") or given.get("note") or given.get("reason") or "")


def task_timeline(viewer: Any, task: Task) -> list[dict[str, Any]]:
    """Versions, submissions, change requests, approvals, cancellation and hand-overs of one task, newest
    first. Entries about a file the viewer cannot see are left out, not blurred."""
    out: list[dict[str, Any]] = []
    links = list(_visible_links(viewer, task).select_related("file", "version", "version__uploaded_by"))
    assignees = set(TaskAssignment.objects.filter(task=task).values_list("user_id", flat=True))
    seen_versions: dict[Any, FileVersion] = {}
    for link in links:
        v = link.version
        seen_versions[v.pk] = v
        label = action_label(v, link, task, assignees)
        out.append(
            {
                "at": v.uploaded_at, "kind": "file", "title": f"{v.uploaded_by.full_name} {label.lower()} "
                f"{v.file.display_name} (version {v.version_number})",
                "label": label, "version": v, "file": v.file, "note": v.note, "who": v.uploaded_by.full_name,
            }
        )  # fmt: skip
    events = (
        TaskEvent.objects.filter(task=task, event_type__in=list(EVENT_WORDS))
        .select_related("actor")
        .order_by("-created_at")[:200]
    )
    for e in events:
        who = e.actor.full_name if e.actor else "The system"
        out.append(
            {
                "at": e.created_at, "kind": "event", "title": f"{who} {EVENT_WORDS[e.event_type]}",
                "label": EVENT_WORDS[e.event_type].split()[0].capitalize(),
                "note": _event_note(e), "who": who,
            }
        )  # fmt: skip
    handovers = FileHandover.objects.filter(version_id__in=list(seen_versions)).select_related("from_user")
    for h in handovers.prefetch_related("recipients__to_user"):
        v = seen_versions.get(h.version_id)
        names = ", ".join(r.to_user.full_name for r in h.recipients.all())
        out.append(
            {
                "at": h.sent_at, "kind": "handover",
                "title": f"{h.from_user.full_name} sent {v.file.display_name if v else 'a file'} to {names}",
                "label": "Sent", "note": h.note, "who": h.from_user.full_name,
            }
        )  # fmt: skip
        for r in h.recipients.all():
            if r.revoked_at:
                out.append(
                    {
                        "at": r.revoked_at, "kind": "revoked", "label": "Stopped", "note": "",
                        "title": f"Sharing {v.file.display_name if v else 'the file'} "
                        f"with {r.to_user.full_name} was stopped",
                        "who": r.revoked_by.full_name if r.revoked_by else "",
                    }
                )  # fmt: skip
            if r.first_opened_at:
                out.append(
                    {
                        "at": r.first_opened_at, "kind": "opened", "label": "Opened", "note": "",
                        "title": f"{r.to_user.full_name} opened {v.file.display_name if v else 'the file'}",
                        "who": r.to_user.full_name,
                    }
                )  # fmt: skip
    out.sort(key=lambda e: e["at"], reverse=True)
    return out


def document_groups(viewer: Any, task: Task) -> list[Document]:
    """The task's documents with their versions oldest first (version 1 on top), for the task's own page."""
    docs: dict[Any, Document] = {}
    assignees = set(TaskAssignment.objects.filter(task=task).values_list("user_id", flat=True))
    links = _visible_links(viewer, task).select_related(
        "file", "version", "version__uploaded_by", "version__blob"
    )
    for link in links.order_by("file__created_at", "version__version_number"):
        v = link.version
        doc = docs.setdefault(link.file_id, Document(link.file))
        doc.rows.append(
            Row(
                v,
                link,
                action_label(v, link, task, assignees),
                _who(v.uploaded_by),
                bool(v.display_name) and v.display_name != v.original_filename,
            )  # fmt: skip
        )
    return list(docs.values())


def tasks_with_files(viewer: Any, limit: int = 50) -> list[Task]:
    ids = _visible_links(viewer).order_by().values("task_id").distinct()
    return list(Task.objects.filter(Q(pk__in=ids)).order_by("-created_at")[:limit])
