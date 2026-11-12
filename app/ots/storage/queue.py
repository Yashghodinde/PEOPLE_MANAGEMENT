"""The storage operations queue (M05 section 4): claim one operation, upload it, finish it, learn from errors.

One operation at a time (memory and bandwidth). The gate (gate.py) says whether anything may run; failures are
classified (classify.py) and turned into a state for the operation. An operation is only DONE after the
provider verified the remote copy, and its completion handler runs in the same transaction as that change.
"""

from __future__ import annotations

import datetime
import hashlib
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from django.db import transaction

from ots.core import alerts, events
from ots.core.clock import now as server_now

from . import classify as c
from .errors import StorageProviderError
from .factory import get_provider
from .gate import Permission, QueueGate
from .gate import gate as default_gate
from .handlers import handler_for
from .models import StorageOperation as Op
from .provider import Library, RemoteMeta, RemoteRef, StorageProvider
from .quickxor import quickxor_file

log = logging.getLogger(__name__)
Clock = Callable[[], datetime.datetime]
STALE_AFTER = datetime.timedelta(minutes=10)
MAX_SESSION_RESTARTS = 5
RESUME_RETRY_SECONDS = 5.0


def enqueue(
    kind: str,
    library: Library,
    local_path: str | Path,
    remote_path: str,
    sha256: str,
    size_bytes: int,
    *,
    ref_type: str = "",
    ref_id: str = "",
) -> Op:
    """Queue an upload. Call it inside the transaction that records the thing being uploaded."""
    return Op.objects.create(
        kind=kind,
        priority=Op.PRIORITY[Op.Kind(kind)],
        target_library=library.value,
        local_path=str(local_path),
        remote_path=remote_path.strip("/"),
        sha256=sha256,
        size_bytes=size_bytes,
        ref_type=ref_type,
        ref_id=ref_id,
        next_attempt_at=server_now(),  # server time from our clock, never the database's
    )


def pinned_local_paths() -> set[str]:
    """Local files that must NOT be evicted from the cache: anything not yet off-site, and dead letters."""
    return set(Op.objects.exclude(state=Op.State.DONE).values_list("local_path", flat=True))


def reset_stale_running(older_than: datetime.timedelta = STALE_AFTER, clock: Clock = server_now) -> int:
    """At worker start: operations a dead worker left RUNNING become RETRY_WAIT (upload state survives)."""
    t = clock()
    return Op.objects.filter(state=Op.State.RUNNING, updated_at__lt=t - older_than).update(
        state=Op.State.RETRY_WAIT, next_attempt_at=t, updated_at=t
    )


# ---- claiming ----
def _allowed(gate: QueueGate) -> tuple[list[str], bool]:
    """Which kinds may run now, and whether running one counts as the probe of a block."""
    allowed, probe = [], False
    for kind in Op.Kind.values:
        d = gate.permission(kind)
        if d.permission != Permission.WAIT:
            allowed.append(kind)
            probe = probe or d.permission == Permission.PROBE
    return allowed, probe


def claim(allowed: list[str], clock: Clock = server_now) -> Op | None:
    """Take the next due operation (priority, then age) and mark it RUNNING. Safe with several workers."""
    t = clock()
    with transaction.atomic():
        op = (
            Op.objects.select_for_update(skip_locked=True)
            .filter(
                state__in=[Op.State.PENDING, Op.State.RETRY_WAIT], next_attempt_at__lte=t, kind__in=allowed
            )
            .order_by("priority", "created_at")
            .first()
        )
        if op is None:
            return None
        op.state = Op.State.RUNNING
        if op.first_attempt_at is None:
            op.first_attempt_at = t
        op.save(update_fields=["state", "first_attempt_at", "updated_at"])
        return op


# ---- finishing ----
def _finish(op: Op, ref: RemoteRef, clock: Clock) -> None:
    """DONE + the completion handler, in one transaction. If the handler raises, nothing is marked done."""
    with transaction.atomic():
        op.state = Op.State.DONE
        op.drive_id, op.item_id, op.etag = ref.drive_id, ref.item_id, ref.etag or ""
        op.upload_url_enc, op.upload_expires_at, op.last_error = "", None, ""
        op.save()
        handler = handler_for(op.kind)
        if handler is not None:
            handler(op, ref)
        else:
            log.warning(
                "no completion handler registered", extra={"event": "storage.no_handler", "kind": op.kind}
            )
        events.publish("storage.operation_done", ("storage_operation", str(op.pk)), {"kind": op.kind})


def _requeue(
    op: Op, state: str, when: datetime.datetime, error: str = "", *, count_attempt: bool = False
) -> None:
    op.state, op.next_attempt_at, op.last_error = state, when, error[:2000]
    if count_attempt:
        op.attempts += 1
    op.save(update_fields=["state", "next_attempt_at", "last_error", "attempts", "updated_at"])


def _dead_letter(op: Op, error: str) -> None:
    op.state, op.last_error = Op.State.FAILED, error[:2000]
    op.save(update_fields=["state", "last_error", "updated_at"])
    events.publish("storage.operation_failed", ("storage_operation", str(op.pk)), {"kind": op.kind})


# ---- adoption (F-ST-6, F-ST-7) ----
def _remote_equals_local(provider: StorageProvider, meta: RemoteMeta, op: Op) -> bool:
    """Same size and same content? Compare QuickXorHash; if Graph gave none, stream and compare SHA-256."""
    if meta.size != op.size_bytes:
        return False
    if meta.quick_xor_hash is not None:
        return meta.quick_xor_hash == quickxor_file(op.local_path)
    digest = hashlib.sha256()
    for chunk in provider.open_read(meta.ref):
        digest.update(chunk)
    return digest.hexdigest() == op.sha256


def _adopt_if_same(provider: StorageProvider, op: Op) -> RemoteMeta | None:
    meta = provider.find_by_path(Library(op.target_library), op.remote_path)
    if meta is not None and not meta.deleted and _remote_equals_local(provider, meta, op):
        return meta
    return None


def _resolve_conflict(provider: StorageProvider, op: Op, clock: Clock) -> str:
    """A file with our name exists (409): ours from an earlier try we lost the answer to, or a stranger."""
    meta = _adopt_if_same(provider, op)
    if meta is not None:
        _finish(op, meta.ref, clock)
        return "adopted"
    msg = f"{op.remote_path} already exists in SharePoint with different content"
    alerts.raise_alert(
        c.ALERT_CONFLICT, "CRITICAL", "storage: a different file has our name", f"{op.pk}: {msg}"
    )
    _dead_letter(op, msg)
    return "failed"


def _resolve_expired_session(provider: StorageProvider, op: Op, clock: Clock) -> str:
    """The upload URL is gone: either the last piece landed and we missed the answer, or we start over."""
    meta = _adopt_if_same(provider, op)
    if meta is not None:
        _finish(op, meta.ref, clock)
        return "adopted"
    op.clear_session()
    if op.attempts + 1 >= MAX_SESSION_RESTARTS:
        _dead_letter(op, "the upload session keeps expiring")
        return "failed"
    _requeue(op, Op.State.PENDING, clock(), "upload session expired; restarting", count_attempt=True)
    return "restarted"


# ---- one operation ----
def _upload(provider: StorageProvider, op: Op) -> RemoteRef:
    path = Path(op.local_path)
    if not path.is_file():
        raise StorageProviderError(f"the local file is missing: {op.local_path}")
    return provider.put(Library(op.target_library), op.remote_path, path, op.size_bytes, op.sha256, op)


def run_operation(op: Op, provider: StorageProvider, gate: QueueGate, *, probe: bool, clock: Clock) -> str:
    """Upload one claimed operation and record the outcome. Returns what happened."""
    try:
        ref = _upload(provider, op)
        _finish(op, ref, clock)
    except Exception as exc:  # noqa: BLE001  (every failure becomes a state; nothing is allowed to escape)
        return _failed(op, exc, provider, gate, probe=probe, clock=clock)
    gate.success(probe=probe)
    return "done"


def _backoff(op: Op, action: c.Action | None = None) -> datetime.timedelta:
    seconds = action.delay if action is not None and action.delay else c.backoff_seconds(op.attempts)
    return datetime.timedelta(seconds=seconds)


def _failed(
    op: Op, exc: BaseException, provider: StorageProvider, gate: QueueGate, *, probe: bool, clock: Clock
) -> str:
    t = clock()
    if not isinstance(exc, StorageProviderError):
        # not a storage error (for example the completion handler failed): the upload itself is fine,
        # so retry later; the next attempt finds the item already there and adopts it
        log.exception("storage operation failed", extra={"event": "storage.op_failed", "op": str(op.pk)})
        _requeue(op, Op.State.RETRY_WAIT, t + _backoff(op), str(exc), count_attempt=True)
        return "retry"
    action = gate.failure(exc, attempts=op.attempts, first_attempt_at=op.first_attempt_at, probe=probe)
    kind = action.kind
    if kind in (c.Kind.BLOCK_AUTH, c.Kind.BLOCK_QUOTA, c.Kind.PAUSE_QUEUE):
        # not this operation's fault: it goes back untouched and the gate holds the whole queue
        _requeue(op, Op.State.PENDING, t, action.message)
        return kind.value.lower()
    if kind == c.Kind.CONFLICT:
        return _guard(lambda: _resolve_conflict(provider, op, clock), op, gate, probe, clock)
    if kind == c.Kind.SESSION_EXPIRED:
        return _guard(lambda: _resolve_expired_session(provider, op, clock), op, gate, probe, clock)
    if kind == c.Kind.FAILED:
        _dead_letter(op, action.message)
        return "failed"
    if kind in (c.Kind.RESUME, c.Kind.RESYNC):
        soon = t + datetime.timedelta(seconds=RESUME_RETRY_SECONDS)
        _requeue(op, Op.State.RETRY_WAIT, soon, action.message, count_attempt=True)
    else:
        _requeue(op, Op.State.RETRY_WAIT, t + _backoff(op, action), action.message, count_attempt=True)
    return "retry"


def _guard(fn: Callable[[], str], op: Op, gate: QueueGate, probe: bool, clock: Clock) -> str:
    """Adopting makes more Graph calls; if one of those fails the operation waits and tries again."""
    try:
        result = fn()
    except Exception as exc:  # noqa: BLE001
        action = gate.failure(exc, attempts=op.attempts, first_attempt_at=op.first_attempt_at, probe=probe)
        _requeue(op, Op.State.RETRY_WAIT, clock() + _backoff(op, action), action.message or str(exc),
                 count_attempt=True)  # fmt: skip
        return "retry"
    if result in ("adopted", "restarted"):
        gate.success(probe=probe)
    return result


# ---- the worker job ----
def process_one(
    provider: StorageProvider | None = None,
    gate: QueueGate | None = None,
    clock: Clock = server_now,
) -> str:
    """One scheduler step. Returns 'idle', 'waited', or what happened to the operation it ran."""
    gate = gate or default_gate
    allowed, probe = _allowed(gate)
    if not allowed:
        return "waited"
    op = claim(allowed, clock)
    if op is None:
        return "idle"
    return run_operation(op, provider or get_provider(), gate, probe=probe, clock=clock)


def process_queue(max_operations: int = 50, **kw: Any) -> int:
    """The `storage.process_queue` job body: work until nothing is due (or a batch limit), one at a time."""
    done = 0
    for _ in range(max_operations):
        if process_one(**kw) in ("idle", "waited"):
            break
        done += 1
    return done
