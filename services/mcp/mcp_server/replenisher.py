"""Centralized workflow replenisher.

Spawns exactly one long-running workflow instance per customer at the start
of a run — each instance is permanently bound to one account and loops
internally through all of that account's credits (via the orchestrator's
per-customer queue) until it reaches target. There's no shared queue to
drain and no concurrency pool to maintain: once the 10 instances are
scheduled, this module just polls the orchestrator for completion.

Previously this ran a continuous deficit-spawn loop (~100 concurrent "slots",
each cycling through many short-lived one-credit-then-terminate instances) —
that model is gone now that one instance owns an account for the whole run.
It also used to terminate+purge+reschedule ("restart-in-place") orphaned
instances under the same instance_id. That's actively wrong under this model:
purging wipes a workflow's durable history, so restarting-in-place would
silently reset a customer's progress to $100 instead of letting Dapr's own
durable-task engine replay the instance from where it left off — which it
already does on its own when a host dies, with no action needed here.

`terminate` itself is still needed, though — `stop()` (and `start()`, to clean
up stragglers from a previous run) call the agent's own
`/agent/instances/{id}/terminate` endpoint for every instance this module
scheduled. Without it, "Stop run" only cancelled this module's own polling
loop and the 10 real workflow instances kept looping and crediting accounts
regardless of what the UI showed.

But `terminate_workflow()` alone isn't reliable either — confirmed live that
Catalyst flips the instance's status to `terminated` immediately, yet the
underlying orchestration keeps scheduling and executing activities (still
crediting accounts) for tens of seconds afterward. So `stop()` also flips
`Orchestrator.stopped` (see orchestrator.py) — every instance's next
`get_next_task` call gets `{done: true}` regardless of terminate_workflow()'s
fate, which is what actually, reliably stops the crediting within one credit
cycle. `terminate_workflow()` is kept alongside it for the demo-visible
"terminated" status in Catalyst, and because most instances aren't affected
by this gotcha every time.

Lives in the MCP server (single instance, pinned to the control nodepool).
The MCP server can't talk to its own Dapr workflow API directly (no sidecar
on the control pool), so the actual `schedule_new_workflow` call is delegated
to any agent pod via its `/schedule-one` HTTP endpoint — stateless on the
agent side, load-balances across replicas.
"""

import asyncio
import logging
import os
import time
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from .orchestrator import Orchestrator

log = logging.getLogger("replenisher")


class Replenisher:
    def __init__(self, orch: "Orchestrator") -> None:
        self._orch = orch
        self._agent_base = os.environ.get(
            "AGENT_HTTP_BASE", "http://host.docker.internal:8000"
        )
        self._task: asyncio.Task | None = None
        self._run_tag: int | None = None
        self._target_concurrency: int = 10
        self._spawn_count: int = 0
        self._instance_ids: list[str] = []
        self._last_snap: dict[str, Any] = {}
        # Throttle schedule calls to stay under Catalyst's per-app-id RPS limit.
        self._schedule_throttle_ms = max(
            0, int(os.environ.get("SCHEDULE_THROTTLE_MS", "50"))
        )

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(
        self,
        customers: int,
        credits_per_customer: int,
        target: int,
    ) -> dict[str, Any]:
        # Terminate any instances left running from a previous run first —
        # otherwise they'd keep crediting into the freshly-reset accounts
        # once `orch.reset()` hands out a new execution_run_id/queues below.
        await self._terminate_all()
        # Reset the queues in-process before we begin scheduling.
        orch_state = await self._orch.reset(
            customers=customers,
            credits_per_customer=credits_per_customer,
            target=target,
        )
        self._run_tag = int(time.time())
        self._target_concurrency = customers
        self._spawn_count = 0
        if self._task is not None and not self._task.done():
            self._task.cancel()
        async with httpx.AsyncClient(timeout=10.0) as client:
            for customer_id in range(1, customers + 1):
                instance_id = f"agent-{customer_id:03d}-r{self._run_tag}"
                if await self._schedule_one(client, instance_id, customer_id):
                    self._spawn_count += 1
                    self._instance_ids.append(instance_id)
                if self._schedule_throttle_ms:
                    await asyncio.sleep(self._schedule_throttle_ms / 1000.0)
        self._task = asyncio.create_task(self._loop(), name="replenisher")
        return {
            "run_tag": self._run_tag,
            "target_concurrency": self._target_concurrency,
            "orchestrator": orch_state,
        }

    async def stop(self) -> dict[str, Any]:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        # Belt-and-suspenders: flip the orchestrator's stopped flag FIRST so
        # every instance's next credit_next call gets {done: true} and
        # self-terminates within one credit cycle, regardless of whether the
        # terminate_workflow() calls below actually take effect on Catalyst's
        # end (observed live: they flip status to "terminated" but don't
        # reliably stop the orchestration from scheduling more activities).
        await self._orch.set_stopped(True)
        await self._terminate_all()
        return self.status()

    async def _terminate_all(self) -> None:
        """Terminate every workflow instance scheduled by the current/last
        run, via the agent's own `/agent/instances/{id}/terminate` endpoint —
        best-effort, a stuck/unreachable instance just logs a warning rather
        than blocking the stop/reset the user asked for."""
        if not self._instance_ids:
            return
        instance_ids, self._instance_ids = self._instance_ids, []
        async with httpx.AsyncClient(timeout=10.0) as client:
            for instance_id in instance_ids:
                try:
                    r = await client.post(
                        f"{self._agent_base}/agent/instances/{instance_id}/terminate"
                    )
                    if r.status_code >= 400:
                        log.warning(
                            "terminate %s returned %s: %s",
                            instance_id, r.status_code, r.text[:200],
                        )
                except Exception as e:  # noqa: BLE001
                    log.warning("terminate %s failed: %s", instance_id, e)

    def status(self) -> dict[str, Any]:
        return {
            "run_tag": self._run_tag,
            "target_concurrency": self._target_concurrency,
            "spawn_count": self._spawn_count,
            "replenisher_running": self.running,
            "orchestrator": self._last_snap,
        }

    async def _loop(self) -> None:
        """Watch the 10 already-scheduled instances run to completion. Each
        one owns its own account's queue and loops internally — nothing here
        spawns new instances; Dapr's durable execution handles pod-kill
        resume for the ones already running."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            while True:
                try:
                    snap = await self._orch.status()
                    # Sweep stale in-flight tasks back to their customer's
                    # queue — the owning instance (still alive, or durably
                    # replayed by Dapr after a host restart) simply re-claims
                    # the task on its next credit_next call.
                    sweep = await self._orch.sweep(timeout_seconds=60.0)
                    snap["released_total"] = sweep["released_total"]
                except Exception as e:  # noqa: BLE001
                    log.warning("orch status read failed: %s", e)
                    await asyncio.sleep(2.0)
                    continue

                self._last_snap = snap
                queue_remaining = int(snap.get("queue_remaining", 0))
                in_flight = int(snap.get("in_flight", 0))

                if queue_remaining == 0 and in_flight == 0:
                    # Reconcile orchestrator's `reported` set against the
                    # DB before declaring done. Catches ghost tx_ids that
                    # the orchestrator counted as applied but that never
                    # landed in the transactions table (rare race when an
                    # activity timed out or a connection dropped mid-credit
                    # under heavy chaos). Ghosts get re-queued and the owning
                    # instance picks them up on its next loop iteration.
                    try:
                        recon = await self._orch.reconcile()
                        if recon.get("ghosts", 0) > 0:
                            log.warning(
                                "reconciliation found %d ghost tx_id(s); requeued %d",
                                recon.get("ghosts"),
                                recon.get("requeued"),
                            )
                            await asyncio.sleep(0.2)
                            continue
                    except Exception as e:  # noqa: BLE001
                        log.warning("reconciliation failed: %s", e)
                    log.info(
                        "replenisher done: applied=%s skipped=%s spawn_count=%s",
                        snap.get("applied_total"),
                        snap.get("skipped_total"),
                        self._spawn_count,
                    )
                    return

                await asyncio.sleep(0.5)

    async def _schedule_one(
        self,
        client: httpx.AsyncClient,
        instance_id: str,
        customer_id: int,
        *,
        quiet: bool = False,
    ) -> bool:
        on_fail = log.debug if quiet else log.warning
        try:
            r = await client.post(
                f"{self._agent_base}/schedule-one",
                json={"instance_id": instance_id, "customer_id": customer_id},
            )
            if r.status_code >= 400:
                on_fail(
                    "schedule %s returned %s: %s",
                    instance_id,
                    r.status_code,
                    r.text[:200],
                )
                return False
            data = r.json()
            return bool(data.get("ok", True))
        except Exception as e:  # noqa: BLE001
            on_fail("schedule %s failed: %s", instance_id, e)
            return False
