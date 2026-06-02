"""In-memory work queue for the bank-heist demo.

Generates `customers * credits_per_customer` tasks (default 10 * 100 = 1000),
hands them out one at a time via `next_task`, and tracks completion via
`report_done`. Lives inside the MCP server process for simplicity.

Each work cycle is scoped to an `execution_runs` row in Postgres so the
demo can be replayed without truncating history — tx_ids recycle across
runs, idempotency is enforced by the composite PK (execution_run_id, tx_id).
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .db import Database


@dataclass
class Task:
    customer_id: int
    tx_id: str
    target: int
    n: int
    execution_run_id: int
    requester: str | None = None


@dataclass
class State:
    execution_run_id: int = 0
    customers: int = 10
    credits_per_customer: int = 100
    target: int = 200
    queue: list[Task] = field(default_factory=list)
    in_flight: dict[str, Task] = field(default_factory=dict)
    in_flight_at: dict[str, float] = field(default_factory=dict)  # tx_id -> claim ts
    by_requester: dict[str, str] = field(default_factory=dict)  # requester -> tx_id
    reported: set[str] = field(default_factory=set)  # tx_ids already reported
    applied: int = 0
    skipped: int = 0
    released: int = 0  # tasks the timeout sweep returned to the queue


class Orchestrator:
    def __init__(self) -> None:
        self._state = State()
        self._lock = asyncio.Lock()
        self._db: "Database | None" = None

    async def bootstrap(self, db: "Database") -> None:
        """Attach the DB and adopt the most-recent execution_run as current.
        Called once on server startup."""
        self._db = db
        run = await db.current_execution_run()
        async with self._lock:
            self._state = State(
                execution_run_id=int(run["id"]),
                customers=int(run["customers"]),
                credits_per_customer=int(run["credits_per_customer"]),
                target=int(run["target"]),
            )
            self._populate()

    def _populate(self) -> None:
        self._state.queue.clear()
        self._state.in_flight.clear()
        self._state.reported.clear()
        self._state.by_requester.clear()
        self._state.in_flight_at.clear()
        self._state.applied = 0
        self._state.skipped = 0
        # Round-robin by step so all customers get credits in parallel.
        for n in range(1, self._state.credits_per_customer + 1):
            for c in range(1, self._state.customers + 1):
                self._state.queue.append(
                    Task(
                        customer_id=c,
                        tx_id=f"tx-c{c}-{n}",
                        target=self._state.target,
                        n=n,
                        execution_run_id=self._state.execution_run_id,
                    )
                )

    async def reset(
        self,
        customers: int = 10,
        credits_per_customer: int = 100,
        target: int = 200,
    ) -> dict[str, Any]:
        if self._db is None:
            raise RuntimeError("Orchestrator.bootstrap() not called")
        run = await self._db.start_execution_run(
            customers=customers,
            credits_per_customer=credits_per_customer,
            target=target,
        )
        async with self._lock:
            self._state = State(
                execution_run_id=int(run["id"]),
                customers=customers,
                credits_per_customer=credits_per_customer,
                target=target,
            )
            self._populate()
            return await self._snapshot_locked()

    async def next_task(self, requester: str | None = None) -> dict[str, Any]:
        """Return the next pending task for `requester`. If the requester
        already has an in-flight task, return the same task — this makes the
        endpoint idempotent across workflow replays."""
        async with self._lock:
            if requester:
                existing_tx = self._state.by_requester.get(requester)
                if existing_tx and existing_tx in self._state.in_flight:
                    task = self._state.in_flight[existing_tx]
                    return self._task_response(task)
            if not self._state.queue:
                return {"done": True}
            task = self._state.queue.pop(0)
            task.requester = requester
            self._state.in_flight[task.tx_id] = task
            self._state.in_flight_at[task.tx_id] = time.time()
            if requester:
                self._state.by_requester[requester] = task.tx_id
            return self._task_response(task)

    async def release_for_retry(self, tx_id: str) -> dict[str, Any]:
        """Return an in-flight task to the front of the queue without marking
        it as reported. Used when a transient error (chaos drop, DB hiccup,
        agent crash mid-credit) means the task didn't actually complete —
        otherwise `report_done` would permanently consume it and the
        customer's last credits would never apply."""
        async with self._lock:
            task = self._state.in_flight.pop(tx_id, None)
            self._state.in_flight_at.pop(tx_id, None)
            if task is None:
                return {"released": 0, "reason": "not in_flight"}
            if task.requester:
                self._state.by_requester.pop(task.requester, None)
            self._state.queue.insert(0, task)
            self._state.released += 1
            return {
                "released": 1,
                "queue_remaining": len(self._state.queue),
                "in_flight": len(self._state.in_flight),
            }

    async def report_done(self, tx_id: str, applied: bool) -> dict[str, Any]:
        """Mark `tx_id` complete. Idempotent: a duplicate ReportDone (e.g.
        from a workflow replay) updates no counters."""
        async with self._lock:
            duplicate = tx_id in self._state.reported
            if not duplicate:
                self._state.reported.add(tx_id)
                task = self._state.in_flight.pop(tx_id, None)
                self._state.in_flight_at.pop(tx_id, None)
                if task and task.requester:
                    self._state.by_requester.pop(task.requester, None)
                if applied:
                    self._state.applied += 1
                else:
                    self._state.skipped += 1
            return {
                "tx_id": tx_id,
                "applied": applied,
                "duplicate": duplicate,
                "queue_remaining": len(self._state.queue),
                "in_flight": len(self._state.in_flight),
                "applied_total": self._state.applied,
                "skipped_total": self._state.skipped,
            }

    @staticmethod
    def _task_response(task: Task) -> dict[str, Any]:
        return {
            "done": False,
            "customer_id": task.customer_id,
            "tx_id": task.tx_id,
            "target": task.target,
            "n": task.n,
            "execution_run_id": task.execution_run_id,
        }

    async def sweep(self, timeout_seconds: float = 30.0) -> dict[str, Any]:
        """Return in_flight tasks older than `timeout_seconds` to the queue.

        Defense against leaked in_flight slots when an activity fails before
        ReportDone lands (e.g. transient MCP/DB errors with no retry, or a
        worker crash mid-task). Returned tasks go to the front of the queue
        so the next replenisher tick reassigns them quickly."""
        async with self._lock:
            now = time.time()
            stale = [
                tx_id
                for tx_id, claimed_at in self._state.in_flight_at.items()
                if now - claimed_at > timeout_seconds
            ]
            for tx_id in stale:
                task = self._state.in_flight.pop(tx_id, None)
                self._state.in_flight_at.pop(tx_id, None)
                if task is None:
                    continue
                if task.requester:
                    self._state.by_requester.pop(task.requester, None)
                # Front of queue → fast retry; otherwise tasks pile up at end
                # while replenisher chews through fresh ones.
                self._state.queue.insert(0, task)
                self._state.released += 1
            return {
                "released": len(stale),
                "released_total": self._state.released,
                "in_flight": len(self._state.in_flight),
            }

    async def status(self) -> dict[str, Any]:
        async with self._lock:
            snap = await self._snapshot_locked()
            run_id = self._state.execution_run_id
        # DB-backed counter — survives workflows that crash post-commit. Done
        # outside the lock so task dispense isn't blocked on the round-trip.
        if self._db is not None and run_id:
            snap["applied_total"] = await self._db.count_transactions(run_id)
        return snap

    async def _snapshot_locked(self) -> dict[str, Any]:
        return {
            "execution_run_id": self._state.execution_run_id,
            "customers": self._state.customers,
            "credits_per_customer": self._state.credits_per_customer,
            "target": self._state.target,
            "queue_remaining": len(self._state.queue),
            "in_flight": len(self._state.in_flight),
            # Overwritten by status() with the DB count; this in-memory value
            # is only seen by reset() snapshots, where it's correctly 0.
            "applied_total": self._state.applied,
            "skipped_total": self._state.skipped,
            "released_total": self._state.released,
        }
