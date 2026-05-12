import asyncio
import contextlib
import logging
import os
import time

import httpx
from dapr.ext.workflow import DaprWorkflowClient, WorkflowRuntime
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .agent import build_agent

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("main")

DAPR_HTTP_PORT = os.environ.get("DAPR_HTTP_PORT", "3500")
# `/healthz/outbound` (not `/healthz`) — the latter requires daprd to have
# discovered our app on port 8000, which it can't do until we bind the port,
# which is what we're trying to wait for. Outbound returns 200 as soon as
# daprd's outbound APIs (state, gRPC) are ready, regardless of app status.
DAPR_HEALTH_URL = f"http://127.0.0.1:{DAPR_HTTP_PORT}/v1.0/healthz/outbound"
DAPR_READY_TIMEOUT_S = int(os.environ.get("DAPR_READY_TIMEOUT_S", "120"))


async def wait_for_dapr() -> None:
    """Poll the daprd sidecar's healthz endpoint until it returns 200.

    Without this, WorkflowRuntime.start() races daprd's gRPC bind on :50001
    and dies after a 10-second timeout, leaving the runtime in a broken state.
    """
    deadline = asyncio.get_event_loop().time() + DAPR_READY_TIMEOUT_S
    async with httpx.AsyncClient() as client:
        while True:
            try:
                r = await client.get(DAPR_HEALTH_URL, timeout=2.0)
                if r.status_code < 400:
                    log.info("dapr sidecar ready (%s)", DAPR_HEALTH_URL)
                    return
            except Exception:
                pass
            if asyncio.get_event_loop().time() >= deadline:
                raise RuntimeError(
                    f"dapr sidecar not ready within {DAPR_READY_TIMEOUT_S}s"
                )
            await asyncio.sleep(1.0)


@contextlib.asynccontextmanager
async def lifespan(app_: FastAPI):
    # Catalyst mode injects DAPR_HTTP_ENDPOINT pointing at a remote sidecar —
    # there's no local daprd to wait on. Only poll in local-sidecar mode.
    if not os.environ.get("DAPR_HTTP_ENDPOINT"):
        await wait_for_dapr()
    # Default thread pool is cpu_count + 4 (~12 on most Macs) which serializes
    # I/O-bound activity execution. Bump it so 100 concurrent agents can
    # actually parallelize their httpx calls to MCP / Catalyst.
    runtime = WorkflowRuntime(
        maximum_concurrent_activity_work_items=int(
            os.environ.get("MAX_CONCURRENT_ACTIVITIES", "200")
        ),
        maximum_concurrent_orchestration_work_items=int(
            os.environ.get("MAX_CONCURRENT_ORCHESTRATIONS", "200")
        ),
        maximum_thread_pool_workers=int(
            os.environ.get("MAX_THREAD_POOL_WORKERS", "128")
        ),
    )
    agent = build_agent()
    # agent.start(runtime, auto_register=True) registers workflows + activities
    # AND starts the runtime worker — do not call runtime.start() again.
    agent.start(runtime=runtime, auto_register=True)
    app_.state.agent = agent
    app_.state.runtime = runtime
    log.info(
        "agent + runtime started (stub=%s)",
        os.environ.get("STUB_LLM", "true").lower() != "false",
    )
    try:
        yield
    finally:
        agent.stop()
        runtime.shutdown()


app = FastAPI(lifespan=lifespan)


class TriggerBody(BaseModel):
    customer_id: int = Field(ge=1)
    target: int = Field(default=200, ge=1, le=10_000)


@app.post("/trigger")
def trigger(body: TriggerBody) -> dict:
    instance_id = f"customer-{body.customer_id}"
    prompt = (
        f"Drain customer {body.customer_id}'s account up to ${body.target}. "
        f"Use $1 credits."
    )
    # dapr-agents 1.x registers the orchestrator as plain `agent_workflow`.
    # DaprWorkflowClient.schedule_new_workflow extracts workflow.__name__ to
    # look up the registered orchestrator, so we pass a proxy with that name.
    # (Older docs reference `dapr.agents.{Name}.workflow` — that was the 0.x
    # naming convention and no longer matches what gets registered.)
    workflow_name = "agent_workflow"

    def _wf_proxy():  # noqa: D401
        pass

    _wf_proxy.__name__ = workflow_name

    wf_client = DaprWorkflowClient()
    returned_id = wf_client.schedule_new_workflow(
        workflow=_wf_proxy,
        # agent_workflow reads `message.get("task")` — the input shape is
        # {"task": str, ...metadata}, NOT a chat-message envelope.
        input={"task": prompt},
        instance_id=instance_id,
    )
    return {"instance_id": returned_id, "workflow": workflow_name}


MCP_HTTP_BASE = os.environ.get("MCP_HTTP_BASE", "http://localhost:9000")


class SpawnAgentsBody(BaseModel):
    agents: int = Field(default=100, ge=1, le=500)
    customers: int = Field(default=10, ge=1, le=100)
    credits_per_customer: int = Field(default=100, ge=1, le=1000)
    target: int = Field(default=200, ge=1, le=10_000)
    # multi: agent does GetNextTask → GetBalance → CreditAccount/skip →
    #        ReportDone (~17 workflow activities)
    # single: agent does ProcessTask once (~7 workflow activities, fewer
    #         WAN roundtrips, less LLM ceremony)
    mode: str = Field(default="multi", pattern="^(multi|single)$")


class _RunState:
    """Tracks one in-progress demo run: target concurrency, slot counters,
    and the replenisher task that maintains the agent pool."""

    def __init__(self) -> None:
        self.run_tag: int | None = None
        self.target_concurrency: int = 100
        self.mode: str = "multi"
        self.slot_counters: dict[int, int] = {}
        self.spawn_count: int = 0
        self.last_status: dict[str, int] = {}
        self.task: asyncio.Task | None = None


_run = _RunState()


def _schedule_one(wf_client: DaprWorkflowClient, instance_id: str, prompt: str) -> bool:
    def _wf_proxy():  # noqa: D401
        pass

    _wf_proxy.__name__ = "agent_workflow"
    try:
        wf_client.schedule_new_workflow(
            workflow=_wf_proxy,
            input={"task": prompt},
            instance_id=instance_id,
        )
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("schedule %s failed: %s", instance_id, e)
        return False


async def _replenish_loop() -> None:
    """Maintain `_run.target_concurrency` workflows in-flight until the
    queue drains. Polls /orch/status every 0.5s and spawns enough single-task
    workflows to fill the deficit. Slot identities (slot-N-task-K) cycle so
    the UI heatmap can map cells to stable slot-N labels."""
    wf_client = DaprWorkflowClient()
    async with httpx.AsyncClient() as client:
        while True:
            try:
                # Reclaim leaked in_flight slots before checking deficit.
                # Without this, an activity that errored after next_task but
                # before report_done would hold a slot forever and stall
                # replenishment.
                await client.post(
                    f"{MCP_HTTP_BASE}/orch/sweep",
                    json={"timeout_seconds": 30.0},
                    timeout=5.0,
                )
                r = await client.get(f"{MCP_HTTP_BASE}/orch/status", timeout=5.0)
                snap = r.json()
            except Exception as e:  # noqa: BLE001
                log.warning("orch status poll failed: %s", e)
                await asyncio.sleep(2.0)
                continue

            _run.last_status = snap
            queue_remaining = int(snap.get("queue_remaining", 0))
            in_flight = int(snap.get("in_flight", 0))

            if queue_remaining == 0 and in_flight == 0:
                log.info(
                    "replenisher done: applied=%s skipped=%s spawn_count=%s",
                    snap.get("applied_total"),
                    snap.get("skipped_total"),
                    _run.spawn_count,
                )
                return

            deficit = _run.target_concurrency - in_flight
            if deficit > 0 and queue_remaining > 0:
                count = min(deficit, queue_remaining)
                slots = sorted(
                    range(1, _run.target_concurrency + 1),
                    key=lambda s: _run.slot_counters.get(s, 0),
                )
                for slot in slots[:count]:
                    k = _run.slot_counters.get(slot, 0) + 1
                    _run.slot_counters[slot] = k
                    instance_id = f"slot-{slot}-task-{k}-r{_run.run_tag}"
                    # `requester=<id>` is parsed by the stub LLM and passed to
                    # GetNextTask so the orchestrator can hand the same task
                    # back on replay (idempotent task assignment).
                    # `mode=<multi|single>` selects the 4-tool dance vs the
                    # one-shot ProcessTask path.
                    prompt = (
                        f"requester={instance_id} mode={_run.mode}: claim and "
                        "process exactly one credit task, then stop."
                    )
                    if _schedule_one(wf_client, instance_id, prompt):
                        _run.spawn_count += 1

            await asyncio.sleep(0.5)


@app.post("/spawn-agents")
async def spawn_agents(body: SpawnAgentsBody) -> dict:
    # Reset orchestrator queue to a known starting state.
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{MCP_HTTP_BASE}/orch/reset",
            json={
                "customers": body.customers,
                "credits_per_customer": body.credits_per_customer,
                "target": body.target,
            },
            timeout=10.0,
        )
        r.raise_for_status()
        orch_state = r.json()

    _run.run_tag = int(time.time())
    _run.target_concurrency = body.agents
    _run.mode = body.mode
    _run.slot_counters.clear()
    _run.spawn_count = 0

    if _run.task is not None and not _run.task.done():
        _run.task.cancel()

    _run.task = asyncio.create_task(_replenish_loop())

    return {
        "run_tag": _run.run_tag,
        "target_concurrency": _run.target_concurrency,
        "mode": _run.mode,
        "orchestrator": orch_state,
    }


@app.get("/run-status")
async def run_status() -> dict:
    return {
        "run_tag": _run.run_tag,
        "target_concurrency": _run.target_concurrency,
        "mode": _run.mode,
        "spawn_count": _run.spawn_count,
        "replenisher_running": _run.task is not None and not _run.task.done(),
        "orchestrator": _run.last_status,
    }


@app.post("/stop-agents")
async def stop_agents() -> dict:
    if _run.task and not _run.task.done():
        _run.task.cancel()
        return {"stopped": True}
    return {"stopped": False, "reason": "no replenisher running"}


@app.get("/status/{instance_id}")
def status(instance_id: str) -> dict:
    wf_client = DaprWorkflowClient()
    state = wf_client.get_workflow_state(instance_id=instance_id)
    if state is None:
        raise HTTPException(404, f"workflow {instance_id} not found")
    failure = getattr(state, "failure_details", None)
    return {
        "instance_id": instance_id,
        "runtime_status": str(state.runtime_status),
        "created_at": str(state.created_at),
        "last_updated_at": str(state.last_updated_at),
        "serialized_output": state.serialized_output,
        "serialized_input": getattr(state, "serialized_input", None),
        "failure": {
            "error_type": getattr(failure, "error_type", None),
            "message": getattr(failure, "message", None),
            "stack_trace": getattr(failure, "stack_trace", None),
        }
        if failure is not None
        else None,
    }


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}
