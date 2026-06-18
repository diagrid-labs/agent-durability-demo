"""Centralized workflow replenisher.

Owns the loop that keeps `target_concurrency` agent workflows in-flight against
the orchestrator queue. Previously lived in each agent pod, which produced a
split-brain when more than one agent replica was up (state-per-pod, status
checks round-robin'd between active/idle pods, replenisher pill flipped).

Now lives in the MCP server (single instance, pinned to the control nodepool).
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
        self._target_concurrency: int = 100
        self._spawn_count: int = 0
        self._slot_counters: dict[int, int] = {}
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
        agents: int,
        customers: int,
        credits_per_customer: int,
        target: int,
    ) -> dict[str, Any]:
        # Reset the queue in-process before we begin scheduling.
        orch_state = await self._orch.reset(
            customers=customers,
            credits_per_customer=credits_per_customer,
            target=target,
        )
        self._run_tag = int(time.time())
        self._target_concurrency = agents
        self._spawn_count = 0
        self._slot_counters.clear()
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = asyncio.create_task(self._loop(), name="replenisher")
        return {
            "run_tag": self._run_tag,
            "target_concurrency": self._target_concurrency,
            "orchestrator": orch_state,
        }

    async def stop(self) -> dict[str, Any]:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        return self.status()

    def status(self) -> dict[str, Any]:
        return {
            "run_tag": self._run_tag,
            "target_concurrency": self._target_concurrency,
            "spawn_count": self._spawn_count,
            "replenisher_running": self.running,
            "orchestrator": self._last_snap,
        }

    async def _loop(self) -> None:
        async with httpx.AsyncClient(timeout=10.0) as client:
            while True:
                try:
                    snap = await self._orch.status()
                    # Sweep stale in-flight tasks back to the queue. With the
                    # replenisher local to MCP, we can call directly.
                    # 60s window lets Catalyst's native activity-level
                    # recovery succeed for most chaos cases before our
                    # orchestrator-supervised restart-in-place kicks in.
                    # The trade-off is slightly slower recovery for
                    # workflows that ARE genuinely stuck, but log volume
                    # and Catalyst gateway load both drop noticeably.
                    sweep = await self._orch.sweep(timeout_seconds=60.0)
                    snap["released_total"] = sweep["released_total"]
                except Exception as e:  # noqa: BLE001
                    log.warning("orch status read failed: %s", e)
                    await asyncio.sleep(2.0)
                    continue

                # Restart-in-place for orphans: terminate + purge the stuck
                # workflow, then re-schedule with the SAME instance_id so the
                # audit trail shows one continuous workflow. If any step
                # fails, the orphan stays released-only; the regular
                # replenisher loop below will spawn-new under a fresh id as
                # a safety net.
                for orphan_id in sweep.get("orphans", []):
                    if await self._restart_in_place(client, orphan_id):
                        self._spawn_count += 1

                self._last_snap = snap
                queue_remaining = int(snap.get("queue_remaining", 0))
                in_flight = int(snap.get("in_flight", 0))

                if queue_remaining == 0 and in_flight == 0:
                    # Reconcile orchestrator's `reported` set against the
                    # DB before declaring done. Catches ghost tx_ids that
                    # the orchestrator counted as applied but that never
                    # landed in the transactions table (rare race when an
                    # activity timed out or a connection dropped mid-credit
                    # under heavy chaos). Ghosts get re-queued and the loop
                    # picks them up on the next tick.
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

                deficit = self._target_concurrency - in_flight
                if deficit > 0 and queue_remaining > 0:
                    count = min(deficit, queue_remaining)
                    slots = sorted(
                        range(1, self._target_concurrency + 1),
                        key=lambda s: self._slot_counters.get(s, 0),
                    )
                    for slot in slots[:count]:
                        k = self._slot_counters.get(slot, 0) + 1
                        self._slot_counters[slot] = k
                        instance_id = (
                            f"agent-{slot:03d}-task-{k}-r{self._run_tag}"
                        )
                        if await self._schedule_one(client, instance_id):
                            self._spawn_count += 1
                        if self._schedule_throttle_ms:
                            await asyncio.sleep(self._schedule_throttle_ms / 1000.0)

                await asyncio.sleep(0.5)

    async def _restart_in_place(
        self, client: httpx.AsyncClient, instance_id: str
    ) -> bool:
        """Terminate + purge + re-schedule the same instance_id.

        On any failure, returns False — the regular replenisher loop will
        spawn-new under a fresh id as a fallback. Three sequential calls so
        all-or-nothing isn't quite achievable; we accept that a partial
        failure may leave a terminated-but-not-purged workflow in Catalyst.
        Idempotent retries on next sweep will eventually clean it up."""
        try:
            r = await client.post(
                f"{self._agent_base}/agent/instances/{instance_id}/terminate",
                timeout=5.0,
            )
            if r.status_code >= 400:
                log.warning(
                    "terminate %s returned %s: %s",
                    instance_id, r.status_code, r.text[:200],
                )
                return False
            r = await client.post(
                f"{self._agent_base}/agent/instances/{instance_id}/purge",
                timeout=5.0,
            )
            if r.status_code >= 400:
                log.warning(
                    "purge %s returned %s: %s",
                    instance_id, r.status_code, r.text[:200],
                )
                return False
            # Catalyst's purge is "fire and forget" — the 200 OK returns
            # before the workflow id is actually reusable in its state layer.
            # Empirically the propagation takes 500-1000ms (variable); a 1s
            # wait skips the noisiest case. Remaining retries handle outliers
            # quietly so the only log line is the final success.
            await asyncio.sleep(1.0)
            for delay_ms in (500, 1000, 2000):
                ok = await self._schedule_one(client, instance_id, quiet=True)
                if ok:
                    log.info("restart-in-place succeeded: %s", instance_id)
                    return True
                await asyncio.sleep(delay_ms / 1000.0)
            log.info("restart-in-place exhausted retries, falling back: %s", instance_id)
            return False
        except Exception as e:  # noqa: BLE001
            log.warning("restart-in-place failed for %s: %s", instance_id, e)
            return False

    async def _schedule_one(
        self, client: httpx.AsyncClient, instance_id: str, *, quiet: bool = False,
    ) -> bool:
        """Schedule a workflow on the agent. `quiet` demotes the failure log
        to DEBUG — used by restart-in-place where Catalyst's first-attempt
        502 is expected and absorbed by the retry loop."""
        prompt = (
            f"requester={instance_id}: claim and process "
            "exactly one credit task, then stop."
        )
        on_fail = log.debug if quiet else log.warning
        try:
            r = await client.post(
                f"{self._agent_base}/schedule-one",
                json={"instance_id": instance_id, "prompt": prompt},
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
