"""Live slot ↔ pod mapping.

Each agent pod stamps its hostname on `process_task` calls; this module records
which pod most recently serviced each heatmap slot. The UI uses it to render
"Kill 1 pod (~N agents)" labels keyed to actual current ownership, and the
pod-kill chaos broadcasts a `slot-state="dead"` WS frame for exactly the slots
owned by the killed pod (no theater).

Entries TTL-expire so dead pods don't linger and so slot ownership doesn't
freeze if a workflow stalls. Subsequent tx events from surviving pods
overwrite the mapping naturally.
"""

import asyncio
import time
from dataclasses import dataclass, field


@dataclass
class _SlotEntry:
    pod: str
    last_seen: float


class SlotTracker:
    def __init__(self, ttl_seconds: float = 60.0) -> None:
        self._ttl = ttl_seconds
        self._lock = asyncio.Lock()
        self._slots: dict[int, _SlotEntry] = {}

    async def record(self, slot: int, pod: str) -> None:
        if not pod or slot is None:
            return
        async with self._lock:
            self._slots[slot] = _SlotEntry(pod=pod, last_seen=time.time())

    async def slots_for_pod(self, pod: str) -> list[int]:
        cutoff = time.time() - self._ttl
        async with self._lock:
            return sorted(
                slot for slot, entry in self._slots.items()
                if entry.pod == pod and entry.last_seen >= cutoff
            )

    async def pod_counts(self) -> dict[str, int]:
        cutoff = time.time() - self._ttl
        out: dict[str, int] = {}
        async with self._lock:
            for entry in self._slots.values():
                if entry.last_seen >= cutoff:
                    out[entry.pod] = out.get(entry.pod, 0) + 1
        return out

    async def snapshot(self) -> dict[int, dict]:
        cutoff = time.time() - self._ttl
        async with self._lock:
            return {
                slot: {"pod": e.pod, "last_seen": e.last_seen}
                for slot, e in self._slots.items()
                if e.last_seen >= cutoff
            }
