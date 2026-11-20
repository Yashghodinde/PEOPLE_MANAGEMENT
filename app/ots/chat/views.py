"""Office Chat pages and actions (IMP-16.9 to 16.12). Thin: each view checks the person, calls a service,
renders. A conversation the person is not in is the same 404 as one that does not exist."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from ots.core.errors import NotFound, OtsError, ValidationFailed
from ots.live.publisher import send as live_send

from . import selectors, services
from .models import Kind, MemberRole


def _enabled() -> None:
    if not getattr(settings, "OTS_CHAT_ENABLED", True):
        raise Http404("Chat is switched off.")


def _uuid(raw: Any, field: str = "id") -> uuid.UUID:
    try:
        return uuid.UUID(str(raw))
    except ValueError:
        raise NotFound("Not found.") from None


def _json(request: HttpRequest) -> dict[str, Any]:
    if request.content_type == "application/json":
        try:
            data = json.loads(request.body or b"{}")
        except ValueError:
            raise ValidationFailed("The request is not valid JSON.") from None
        return data if isinstance(data, dict) else {}
    return {k: (v if len(v) > 1 else v[0]) for k, v in request.POST.lists()}


def _fail(exc: OtsError) -> JsonResponse:
    return JsonResponse({"ok": False, "message": str(exc)}, status=getattr(exc, "status", 422))


def _day_label(d: Any, today: Any) -> str:
    if d == today:
        return "Today"
    if (today - d).days == 1:
        return "Yesterday"
    return f"{d.day} {d:%B %Y}"


def decorate(messages: list[Any], mem: Any, viewer: Any) -> list[dict[str, Any]]:
    """Messages with what the page needs: side, grouping, date separators, status, reactions, who may edit."""
    from ots.core.clock import IST, now

    today = now().astimezone(IST).date()
    seen = selectors.other_read_cursor(mem)
    group = mem.conversation.kind == Kind.GROUP
    out: list[dict[str, Any]] = []
    prev_day = prev_sender = None
    prev_system = True
    for m in messages:
        day = m.created_at.astimezone(IST).date()
        mine = m.sender_id == viewer.pk
        reactions: dict[str, dict[str, Any]] = {}
        for r in m.reactions.all():
            slot = reactions.setdefault(r.emoji, {"emoji": r.emoji, "n": 0, "mine": False})
            slot["n"] += 1
            slot["mine"] = slot["mine"] or r.user_id == viewer.pk
        system = m.kind == "SYSTEM"
        status = ("Read" if seen and m.pk <= seen else "Sent") if (mine and not group and not system) else ""
        sig = f"{m.edited_at}|{m.deleted_at}|{status}|" + ",".join(
            f"{r['emoji']}{r['n']}{int(r['mine'])}" for r in reactions.values()
        )
        out.append(
            {
                "sig": sig,
                "m": m,
                "mine": mine,
                "system": system,
                "sep": _day_label(day, today) if day != prev_day else "",
                "show_name": group
                and not mine
                and not system
                and (m.sender_id != prev_sender or day != prev_day),
                "status": ("Read" if seen and m.pk <= seen else "Sent")
                if (mine and not group and not system)
                else "",
                "reactions": list(reactions.values()),
                "can_edit": mine
                and m.kind == "TEXT"
                and m.deleted_at is None
                and (now() - m.created_at).total_seconds() <= services.EDIT_WINDOW_SECONDS,
                "can_delete": mine and m.deleted_at is None and not system,
                "files": [] if m.deleted_at else list(m.attachments.all()),
                "cont": (not system) and (not prev_system) and m.sender_id == prev_sender and day == prev_day,
            }
        )
        prev_day, prev_sender, prev_system = day, m.sender_id, system
    return out


def _page_context(request: HttpRequest, conv_id: Any = None, **extra: Any) -> dict[str, Any]:
    q = request.GET.get("q", "")[:60]
    kind = request.GET.get("kind", "")
    ctx: dict[str, Any] = {
        "page_title": "Chat",
        "rows": selectors.conversation_rows(request.user, kind, q),
        "kind": kind,
        "q": q,
        "conv": None,
        "reactions": services.REACTIONS,
    }
    if conv_id is not None:
        mem = services.membership(request.user, conv_id)
        services.mark_read(request.user, conv_id)
        msgs, more = selectors.history(mem)
        title = next((r.title for r in ctx["rows"] if r.conv.pk == mem.conversation_id), "")
        if not title:
            rows = selectors.conversation_rows(request.user, "", "")
            title = next((r.title for r in rows if r.conv.pk == mem.conversation_id), "Conversation")
        members = selectors.members_of(mem.conversation)
        other = (
            next((x.user for x in members if x.user_id != request.user.pk), None)
            if mem.conversation.kind == Kind.DIRECT
            else None
        )
        ctx.update(
            conv=mem.conversation,
            mem=mem,
            title=title,
            other=other,
            members=members,
            more=more,
            items=decorate(msgs, mem, request.user),
            is_admin=mem.role in (MemberRole.OWNER, MemberRole.ADMIN),
            page_title=title,
            crumbs=[("Chat", "/app/chat"), (title, "")],
            last_id=msgs[-1].pk if msgs else "",
            first_id=msgs[0].pk if msgs else "",
        )
    ctx.update(extra)
    return ctx


@login_required
@require_http_methods(["GET"])
def home(request: HttpRequest) -> HttpResponse:
    _enabled()
    return render(request, "chat/home.html", _page_context(request))


@login_required
@require_http_methods(["GET"])
def conversation(request: HttpRequest, conv_id: uuid.UUID) -> HttpResponse:
    _enabled()
    return render(request, "chat/home.html", _page_context(request, conv_id))


@login_required
@require_http_methods(["GET"])
def conversation_files(request: HttpRequest, conv_id: uuid.UUID) -> HttpResponse:
    _enabled()
    mem = services.membership(request.user, conv_id)
    newest = request.GET.get("order") == "newest"
    ctx = _page_context(
        request, conv_id, tab="files", files=selectors.shared_files(mem, newest), newest=newest
    )
    return render(request, "chat/home.html", ctx)


@login_required
@require_http_methods(["GET"])
def list_fragment(request: HttpRequest) -> HttpResponse:
    """The sidebar list on its own, for a live refresh."""
    _enabled()
    ctx = _page_context(request)
    ctx["active"] = request.GET.get("active", "")
    return render(request, "chat/_list.html", ctx)


@login_required
@require_http_methods(["GET"])
def messages_fragment(request: HttpRequest, conv_id: uuid.UUID) -> HttpResponse:
    """Older messages (`before`) or newer ones (`after`) as rows, oldest first."""
    _enabled()
    mem = services.membership(request.user, conv_id)
    before, after = request.GET.get("before") or None, request.GET.get("after") or None
    msgs, more = selectors.history(mem, before=before, after=after)
    resp = render(
        request,
        "chat/_messages.html",
        {
            "items": decorate(msgs, mem, request.user),
            "conv": mem.conversation,
            "reactions": services.REACTIONS,
        },
    )
    resp["X-Has-More"] = "1" if more else "0"
    return resp


@login_required
@require_http_methods(["POST"])
def send(request: HttpRequest, conv_id: uuid.UUID) -> JsonResponse:
    _enabled()
    try:
        d = _json(request)
        vids = d.get("version_ids") or []
        vids = vids if isinstance(vids, list) else [vids]
        kind = "FILE" if vids and not str(d.get("body", "")).strip() else "TEXT"
        cid = d.get("client_id") or request.headers.get("Idempotency-Key")
        m, created = services.send(
            request.user,
            conv_id,
            str(d.get("body", "")),
            client_id=_uuid(cid) if cid else None,
            reply_to=d.get("reply_to") or None,
            kind=kind,
            version_ids=[str(v) for v in vids],
        )
    except OtsError as exc:
        return _fail(exc)
    return JsonResponse({"ok": True, "id": str(m.pk), "created": created}, status=201 if created else 200)


@login_required
@require_http_methods(["POST"])
def read(request: HttpRequest, conv_id: uuid.UUID) -> JsonResponse:
    _enabled()
    try:
        services.mark_read(request.user, conv_id, _json(request).get("upto") or None)
    except OtsError as exc:
        return _fail(exc)
    return JsonResponse({"ok": True, "unread": selectors.unread_conversation_count(request.user)})


@login_required
@require_http_methods(["POST"])
def react(request: HttpRequest, message_id: uuid.UUID) -> JsonResponse:
    _enabled()
    try:
        on = services.react(request.user, message_id, str(_json(request).get("emoji", "")))
    except OtsError as exc:
        return _fail(exc)
    return JsonResponse({"ok": True, "on": on})


@login_required
@require_http_methods(["POST"])
def edit(request: HttpRequest, message_id: uuid.UUID) -> JsonResponse:
    _enabled()
    try:
        m = services.edit(request.user, message_id, str(_json(request).get("body", "")))
    except OtsError as exc:
        return _fail(exc)
    return JsonResponse({"ok": True, "id": str(m.pk)})


@login_required
@require_http_methods(["POST"])
def delete(request: HttpRequest, message_id: uuid.UUID) -> JsonResponse:
    _enabled()
    try:
        services.delete(request.user, message_id)
    except OtsError as exc:
        return _fail(exc)
    return JsonResponse({"ok": True})


@login_required
@require_http_methods(["GET"])
def unread(request: HttpRequest) -> JsonResponse:
    _enabled()
    return JsonResponse({"count": selectors.unread_conversation_count(request.user)})


@login_required
@require_http_methods(["GET"])
def people(request: HttpRequest) -> JsonResponse:
    _enabled()
    rows = selectors.people(request.user, request.GET.get("q", ""))
    return JsonResponse(
        {
            "people": [
                {
                    "id": str(u.pk),
                    "name": u.full_name,
                    "employee_id": u.employee_id,
                    "designation": u.designation or u.get_role_display(),
                }
                for u in rows
            ]
        }
    )


@login_required
@require_http_methods(["POST"])
def new_chat(request: HttpRequest) -> JsonResponse:
    """One person opens (or makes) the private chat; several make a group."""
    _enabled()
    try:
        d = _json(request)
        ids = d.get("people") or []
        ids = ids if isinstance(ids, list) else [ids]
        if len(ids) == 1:
            conv = services.open_direct(request.user, _uuid(ids[0]))
        else:
            conv = services.create_group(request.user, str(d.get("title", "")), [_uuid(i) for i in ids])
    except OtsError as exc:
        return _fail(exc)
    return JsonResponse({"ok": True, "url": f"/app/chat/c/{conv.pk}"}, status=201)


@login_required
@require_http_methods(["POST"])
def manage(request: HttpRequest, conv_id: uuid.UUID) -> JsonResponse:
    """Group and personal settings in one endpoint, by `op`."""
    _enabled()
    d, u = _json(request), request.user
    try:
        op = d.get("op")
        if op == "add":
            ids = d.get("people") or []
            services.add_members(
                u,
                conv_id,
                [_uuid(i) for i in (ids if isinstance(ids, list) else [ids])],
                share_history=bool(d.get("history")),
            )
        elif op == "remove":
            services.remove_member(u, conv_id, _uuid(d.get("user")))
        elif op == "leave":
            services.remove_member(u, conv_id, u.pk)
        elif op == "rename":
            services.rename_group(u, conv_id, str(d.get("title", "")))
        elif op == "role":
            services.set_role(u, conv_id, _uuid(d.get("user")), str(d.get("role", "")))
        elif op == "mute":
            services.set_muted(u, conv_id, bool(d.get("on")))
        elif op == "pin":
            services.set_pinned(u, conv_id, bool(d.get("on")))
        elif op == "archive":
            services.archive(u, conv_id, bool(d.get("on", True)))
        else:
            raise ValidationFailed("Choose an action.")
    except OtsError as exc:
        return _fail(exc)
    return JsonResponse({"ok": True})


_typing: dict[tuple[Any, Any], float] = {}


@login_required
@require_http_methods(["POST"])
def typing(request: HttpRequest, conv_id: uuid.UUID) -> JsonResponse:
    """Someone is typing: announced to the others, never stored, at most once every two seconds."""
    _enabled()
    try:
        services.membership(request.user, conv_id)
    except OtsError as exc:
        return _fail(exc)
    key, t = (request.user.pk, conv_id), time.monotonic()
    if t - _typing.get(key, 0) >= 2:
        _typing[key] = t
        if len(_typing) > 5000:
            _typing.clear()
        live_send(
            users=[u for u in services._audience_ids(conv_id, request.user)],
            type_="chat",
            conversation_id=conv_id,
            what="typing",
            who=request.user.full_name,
        )
    return JsonResponse({"ok": True})


@login_required
@require_http_methods(["GET"])
def attachable(request: HttpRequest) -> JsonResponse:
    """The person's own recent files and the documents of their File IDs, for the attach picker."""
    _enabled()
    from ots.files.models import File, FileStatus, FileVisibility
    from ots.workspaces.models import FileIdDocument
    from ots.workspaces.selectors import workspaces_visible_to

    kind = request.GET.get("from", "storage")
    if kind == "fileid":
        docs = (
            FileIdDocument.objects.filter(
                removed_at__isnull=True, workspace__in=workspaces_visible_to(request.user).values("pk")
            )
            .select_related("file__current_version", "workspace")
            .order_by("-added_at")[:60]
        )
        items = [
            {
                "version": str(d.file.current_version_id),
                "name": d.file.display_name,
                "where": d.workspace.name,
            }
            for d in docs
            if d.file.current_version_id
        ]
    else:
        files = File.objects.filter(
            owner=request.user, visibility=FileVisibility.PRIVATE, status=FileStatus.ACTIVE
        )
        files = files.select_related("current_version").order_by("-created_at")[:60]
        items = [
            {"version": str(f.current_version_id), "name": f.display_name, "where": "My storage"}
            for f in files
            if f.current_version_id and not f.current_version.is_voice
        ]
    return JsonResponse({"items": items})


@login_required
@require_http_methods(["GET"])
def search(request: HttpRequest) -> HttpResponse:
    _enabled()
    q = request.GET.get("q", "")[:80]
    ctx = _page_context(request)
    ctx.update(
        q=q,
        results=selectors.search_messages(request.user, q),
        file_results=selectors.search_files(request.user, q),
        people_results=selectors.people(request.user, q, 8) if len(q.strip()) >= 2 else [],
        convo_results=[r for r in ctx["rows"] if q.strip() and q.strip().lower() in r.title.lower()],
        page_title="Search chat",
    )
    return render(request, "chat/search.html", ctx)


__all__ = ["redirect"]
