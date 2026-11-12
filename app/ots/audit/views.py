"""Audit viewer (/audit), CSV export, and the per-person activity timeline."""

from __future__ import annotations

import csv
import datetime
import json
import uuid
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpRequest, HttpResponse, StreamingHttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_http_methods

from ots.accounts.models import Role, User
from ots.core import permissions
from ots.core.clock import IST, now
from ots.core.errors import NotFound

from . import api, selectors, timeline


def _uuid_or_none(raw: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(raw).strip())
    except (ValueError, AttributeError, TypeError):
        return None


def _date_or_none(raw: Any) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(str(raw).strip())
    except (ValueError, TypeError):
        return None


def _clean(raw: Any, limit: int) -> str:
    """Text from the query string: no control characters (Postgres rejects NUL), trimmed, bounded."""
    return "".join(ch for ch in str(raw or "") if ch.isprintable()).strip()[:limit]


def _filter_from(params: Any) -> selectors.AuditFilter:
    return selectors.AuditFilter(
        actor=_uuid_or_none(params.get("actor")),
        action_prefix=_clean(params.get("action"), 80),
        date_from=_date_or_none(params.get("from")),
        date_to=_date_or_none(params.get("to")),
        target=_clean(params.get("target"), 64),
    )


def _filter_query(params: Any) -> list[tuple[str, str]]:
    """The filter parts of the query string, so paging and export keep them."""
    wanted = [(k, _clean(params.get(k), 80)) for k in ("actor", "action", "from", "to", "target")]
    return [(k, v) for k, v in wanted if v]


def _who(ids: set[Any]) -> dict[str, str]:
    """actor id -> name, in one query, for the rows on screen."""
    users = User.objects.filter(pk__in=[i for i in ids if i]).values_list("pk", "full_name", "login_id")
    return {str(pk): f"{name} ({login})" for pk, name, login in users}


def _about(row: Any, names: dict[str, str]) -> str:
    """What the record is about: a person by name, anything else as 'type id'."""
    if not row.target_type and not row.target_id:
        return "—"
    if row.target_type == "user" and row.target_id in names:
        return names[row.target_id]
    return f"{row.target_type} {row.target_id}".strip()


def _local(at: datetime.datetime) -> str:
    return at.astimezone(IST).strftime("%d %b %Y, %H:%M:%S")


def _payload_text(payload: Any, limit: int = 120) -> str:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True) if payload else ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


@login_required
@require_http_methods(["GET"])
def audit_list(request: HttpRequest) -> HttpResponse:
    permissions.require(request.user, "audit.view")
    f = _filter_from(request.GET)
    cursor = selectors.RowCursor.decode(request.GET.get("cursor"))
    page = selectors.audit_page(request.user, f, cursor)
    names = _who({r.actor_id for r in page.rows} | {_uuid_or_none(r.target_id) for r in page.rows})
    rows = [
        [
            _local(r.at),
            names.get(str(r.actor_id), "—" if r.actor_id is None else str(r.actor_id)[:8]),
            r.action,
            _about(r, names),
            r.client_ip or "—",
            _payload_text(r.payload) or "—",
        ]
        for r in page.rows
    ]
    qs = _filter_query(request.GET)
    is_admin = request.user.role != Role.EMPLOYEE
    ctx = {
        "page_title": "Audit log" if is_admin else "My sign-in history",
        "columns": [{"label": c} for c in ("Time", "Who", "Action", "About", "From", "Details")],
        "rows": rows,
        "f": {
            "actor": request.GET.get("actor", ""),
            "action": f.action_prefix,
            "from": request.GET.get("from", ""),
            "to": request.GET.get("to", ""),
            "target": f.target,
        },
        "actors": list(User.objects.visible_to(request.user).order_by("full_name")) if is_admin else [],
        "is_admin": is_admin,
        "next_url": "?" + urlencode([*qs, ("cursor", page.next_cursor.encode())]) if page.next_cursor else "",
        "export_url": "/audit/export?" + urlencode(qs) if qs else "/audit/export",
    }
    return render(request, "audit/audit_list.html", ctx)


class _Echo:
    def write(self, value: str) -> str:
        return value


def _safe_cell(value: Any) -> str:
    """Stop spreadsheet formula injection: logged text such as an attacker-typed login ID must not execute."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def _csv_rows(request: HttpRequest, f: selectors.AuditFilter) -> Iterator[str]:
    writer = csv.writer(_Echo())
    yield writer.writerow(
        [
            "id",
            "time_ist",
            "actor_id",
            "actor",
            "action",
            "target_type",
            "target_id",
            "client_ip",
            "request_id",
            "details",
        ]
    )
    names = _who(set(User.objects.visible_to(request.user).values_list("pk", flat=True)))
    for r in selectors.filtered(request.user, f).iterator(chunk_size=500):
        yield writer.writerow(
            [
                r.id,
                r.at.astimezone(IST).isoformat(timespec="seconds"),
                r.actor_id or "",
                _safe_cell(names.get(str(r.actor_id), "")),
                _safe_cell(r.action),
                _safe_cell(r.target_type),
                _safe_cell(r.target_id),
                r.client_ip or "",
                _safe_cell(r.request_id),
                _safe_cell(json.dumps(r.payload, ensure_ascii=False, sort_keys=True) if r.payload else ""),
            ]
        )


@login_required
@require_http_methods(["GET"])
def audit_export(request: HttpRequest) -> HttpResponse:
    """The filtered result as a CSV, streamed. Same scope as the screen; the export itself is audited."""
    permissions.require(request.user, "audit.view")
    f = _filter_from(request.GET)
    with transaction.atomic():
        api.record(
            "audit.exported",
            request.user,
            None,
            {k: v for k, v in _filter_query(request.GET)},
            request,
        )
    response = StreamingHttpResponse(_csv_rows(request, f), content_type="text/csv; charset=utf-8")
    stamp = now().astimezone(IST).strftime("%Y%m%d-%H%M")
    response["Content-Disposition"] = f'attachment; filename="audit-{stamp}.csv"'
    response["Cache-Control"] = "no-store"
    return response


@login_required
@require_http_methods(["GET"])
def person_activity(request: HttpRequest, pk: Any) -> HttpResponse:
    """The Activity tab: one person's merged timeline, loaded into the profile with HTMX."""
    permissions.require(request.user, "audit.view")
    target = User.objects.visible_to(request.user).filter(pk=pk).first()
    if target is None:
        raise NotFound("Person not found.")
    cursor = timeline.Cursor.decode(request.GET.get("cursor"))
    page = timeline.activity_timeline(
        target.pk,
        _date_or_none(request.GET.get("from")),
        _date_or_none(request.GET.get("to")),
        cursor=cursor,
    )
    qs = _filter_query(request.GET)
    more = (
        f"/app/people/{target.pk}/activity?"
        + urlencode([(k, v) for k, v in qs if k in ("from", "to")] + [("cursor", page.next_cursor.encode())])
        if page.next_cursor
        else ""
    )
    return render(
        request,
        "audit/_activity.html",
        {"events": page.events, "next_url": more, "append": cursor is not None, "person": target},
    )
