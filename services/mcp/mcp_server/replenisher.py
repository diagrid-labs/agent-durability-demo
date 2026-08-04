"""Centralized workflow replenisher.

Spawns one long-running instance per customer at run start, each permanently
bound to that account and looping internally through its credits until
target. No shared queue or concurrency pool to maintain — once the 10
instances are scheduled, this module just polls for completion.

Never restarts-in-place: purging a workflow wipes its durable history, so
resuming under the same instance_id would reset progress to $100. Dapr's own
durable-task engine already replays a dead host's instances on its own.

`terminate` is still needed for `stop()`/`start()` cleanup — without it, "Stop
run" only cancels this module's polling loop and the real instances keep
crediting. But `terminate_workflow()` alone is unreliable: Catalyst marks an
instance `terminated` immediately while the underlying orchestration keeps
crediting for tens of seconds after. So `stop()` also flips
`Orchestrator.stopped` — every instance's next `get_next_task` gets
`{done: true}` regardless, which is what actually stops crediting within one
cycle. `terminate_workflow()` stays too, for the demo-visible status.

Lives in the MCP server, which has no Dapr sidecar of its own (control
nodepool) — so scheduling is delegated to any agent pod's `/schedule-one`.

Scheduling goes straight to each pod's IP (round-robin), not through the
`agent` Service: one httpx.AsyncClient's keep-alive connection would pin all
10 calls to whichever pod the first request landed on, so pod-kill chaos
would hit all-or-nothing instead of a partial freeze. Falls back to
`AGENT_HTTP_BASE` when pod discovery isn't available (e.g. local compose).
"""

import asyncio
import logging
import os
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx

if TYPE_CHECKING:
    from .orchestrator import Orchestrator
    from .pod_chaos import PodChaosController

log = logging.getLogger("replenisher")


class Replenisher:
    def __init__(self, orch: "Orchestrator", pod_chaos: "PodChaosController | None" = None) -> None:
        self._orch = orch
        self._pod_chaos = pod_chaos
        self._agent_base = os.environ.get(
            "AGENT_HTTP_BASE", "http://host.docker.internal:8000"
        )
        self._agent_port = urlsplit(self._agent_base).port or 8000
        self._task: asyncio.Task | None = None
        self._run_tag: int | None = None
        self._target_concurrency: int = 10
        self._spawn_count: int = 0
        self._instance_ids: list[str] = []
        self._last_snap: dict[str, Any] = {}
        # Stay under Catalyst's per-app-id RPS limit.
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
        # Clear stragglers first — otherwise they'd credit into the accounts
        # `orch.reset()` is about to hand fresh queues to.
        await self._terminate_all()
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
        pod_ips = self._live_agent_pod_ips()
        if pod_ips:
            log.info("scheduling across %d live agent pod(s): %s", len(pod_ips), pod_ips)
        # 45s: a pod's first /schedule-one (building its runtime) can take
        # well past 10s under load — confirmed live on bank-creditor-dapr-agents.
        async with httpx.AsyncClient(timeout=45.0) as client:
            for customer_id in range(1, customers + 1):
                instance_id = f"agent-{customer_id:03d}-r{self._run_tag}"
                base_url = (
                    f"http://{pod_ips[(customer_id - 1) % len(pod_ips)]}:{self._agent_port}"
                    if pod_ips
                    else self._agent_base
                )
                if await self._schedule_one(client, base_url, instance_id, customer_id):
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
        # Flip stopped first so every instance self-terminates within one
        # credit cycle regardless of whether terminate_workflow() below
        # actually takes effect (see module docstring).
        await self._orch.set_stopped(True)
        await self._terminate_all()
        return self.status()

    async def _terminate_all(self) -> None:
        """Terminate every instance from the current/last run via each
        instance's `/agent/instances/{id}/terminate` — best-effort, a stuck
        instance just logs a warning.

        Broadcasts to every live pod, not just the one the Service would
        route to: necessary for `agent-plain`, whose `_tasks` registry is
        per-pod in-memory, so an instance only exists on the pod that
        scheduled it."""
        if not self._instance_ids:
            return
        instance_ids, self._instance_ids = self._instance_ids, []
        pod_ips = self._live_agent_pod_ips()
        bases = [f"http://{ip}:{self._agent_port}" for ip in pod_ips] or [self._agent_base]
        async with httpx.AsyncClient(timeout=10.0) as client:
            for instance_id in instance_ids:
                for base_url in bases:
                    try:
                        r = await client.post(
                            f"{base_url}/agent/instances/{instance_id}/terminate"
                        )
                        if r.status_code >= 400:
                            log.debug(
                                "terminate %s via %s returned %s: %s",
                                instance_id, base_url, r.status_code, r.text[:200],
                            )
                    except Exception as e:  # noqa: BLE001
                        log.debug("terminate %s via %s failed: %s", instance_id, base_url, e)

    def _live_agent_pod_ips(self) -> list[str]:
        """Live agent pod IPs for direct (non-Service) requests. Empty when
        pod discovery isn't available (not in-cluster, or no pod_chaos
        instance wired in) — callers fall back to `AGENT_HTTP_BASE`."""
        if self._pod_chaos is None:
            return []
        try:
            pods = self._pod_chaos.list_live_pods()
        except Exception:  # noqa: BLE001
            return []
        return sorted(p["ip"] for p in pods if p.get("ip") and p.get("phase") == "Running")

    def status(self) -> dict[str, Any]:
        return {
            "run_tag": self._run_tag,
            "target_concurrency": self._target_concurrency,
            "spawn_count": self._spawn_count,
            "replenisher_running": self.running,
            "orchestrator": self._last_snap,
        }

    async def _loop(self) -> None:
        """Watch the 10 already-scheduled instances run to completion.
        Nothing here spawns new instances — Dapr's durable execution handles
        pod-kill resume for the ones already running."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            while True:
                try:
                    snap = await self._orch.status()
                    # Stale in-flight tasks go back to their queue; the owning
                    # instance re-claims on its next credit_next call.
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
                    # Reconcile against the DB before declaring done — catches
                    # ghost tx_ids counted as applied but never persisted
                    # (rare race under heavy chaos). Ghosts get re-queued.
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
        base_url: str,
        instance_id: str,
        customer_id: int,
        *,
        quiet: bool = False,
    ) -> bool:
        on_fail = log.debug if quiet else log.warning
        try:
            r = await client.post(
                f"{base_url}/schedule-one",
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
