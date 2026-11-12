"""A stand-in for the M05.4 queue: ask the gate, make one provider call, report the result to the gate.

It follows exactly the contract the real queue follows, so the F-ST tests prove the gate and survive M05.4.
"""

from __future__ import annotations

import datetime
from typing import Any

from ots.storage.errors import StorageProviderError
from ots.storage.gate import Permission, QueueGate


def run_once(
    gate: QueueGate,
    provider: Any,
    *,
    attempts: int = 0,
    first: datetime.datetime | None = None,
    kind: str = "",
) -> str:
    """One scheduler tick. Returns 'ok', 'waited', or the action kind that a failure produced."""
    decision = gate.permission(kind)
    if decision.permission == Permission.WAIT:
        return "waited"
    probe = decision.permission == Permission.PROBE
    try:
        provider.usage()  # any Graph call stands in for the operation
    except StorageProviderError as exc:
        return gate.failure(exc, attempts=attempts, first_attempt_at=first, probe=probe).kind.value
    gate.success(probe=probe)
    return "ok"
