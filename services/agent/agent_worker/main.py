import asyncio
import contextlib
import logging
import os

import httpx
from dapr.ext.workflow import DaprWorkflowClient
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .agent import build_runner

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("main")

DAPR_HTTP_PORT = os.environ.get("DAPR_HTTP_PORT", "3500")
# `/healthz/outbound` not `/healthz`: the latter requires daprd to have
# discovered our app on port 8000, which we haven't bound yet.
DAPR_HEALTH_URL = f"http://127.0.0.1:{DAPR_HTTP_PORT}/v1.0/healthz/outbound"
DAPR_READY_TIMEOUT_S = int(os.environ.get("DAPR_READY_TIMEOUT_S", "120"))


async def wait_for_dapr() -> None:
    """Poll the daprd sidecar's healthz endpoint until it returns 200.

    Without this, the Dapr workflow runtime races daprd's gRPC bind on
    :50001 and dies after a 10-second timeout, leaving it in a broken state.
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
    runner = build_runner()
    runner.start()
    app_.state.runner = runner
    log.info(
        "runner started (stub=%s)",
        os.environ.get("STUB_LLM", "true").lower() != "false",
    )
    try:
        yield
    finally:
        runner.shutdown()


app = FastAPI(lifespan=lifespan)


class TriggerBody(BaseModel):
    customer_id: int = Field(ge=1)
    target: int = Field(default=200, ge=1, le=10_000)


class ScheduleOneBody(BaseModel):
    instance_id: str = Field(min_length=1)
    customer_id: int = Field(ge=1)


async def _schedule(instance_id: str, customer_id: int) -> None:
    """Durably schedule one graph run under `instance_id` and return as soon
    as Catalyst confirms it's been accepted — mirrors the crash-recovery
    quickstart's `/run` handler, which returns on the first `workflow_started`
    event rather than waiting for the graph to finish.

    `workflow_id` (not `thread_id`) is what becomes the actual Dapr workflow
    instance ID that `/status`, `/agent/instances/{id}/terminate`, and
    `/agent/instances/{id}/purge` key off of — DaprWorkflowGraphRunner
    defaults it to a random `graph-<thread_id>-<uuid>` string otherwise.

    The initial state is the fixed-size BankerState shape (agent.py) — no
    chat prompt to parse; `customer_id` is passed straight through as a
    structured field."""
    runner = app.state.runner
    events = runner.run_async(
        input={
            "requester": instance_id,
            "customer_id": customer_id,
            "last_result": None,
            "done": False,
            "final_message": None,
        },
        thread_id=instance_id,
        workflow_id=instance_id,
    )
    event = await events.__anext__()
    await events.aclose()
    if event.get("type") != "workflow_started":
        raise RuntimeError(f"unexpected first event: {event}")


@app.post("/schedule-one")
async def schedule_one(body: ScheduleOneBody) -> dict:
    """Stateless workflow scheduler. The MCP-side replenisher posts here for
    each workflow it wants to start; this pod's local Dapr sidecar handles
    the schedule_new_workflow gRPC call. No in-process state — any agent
    replica can serve this and Dapr's placement service routes the workflow
    onto whichever agent ends up hosting it."""
    try:
        await _schedule(body.instance_id, body.customer_id)
        return {"ok": True, "instance_id": body.instance_id}
    except Exception as e:  # noqa: BLE001
        log.warning("schedule %s failed: %s", body.instance_id, e)
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/trigger")
async def trigger(body: TriggerBody) -> dict:
    instance_id = f"customer-{body.customer_id}"
    try:
        await _schedule(instance_id, body.customer_id)
    except Exception as e:  # noqa: BLE001
        log.warning("trigger %s failed: %s", instance_id, e)
        raise HTTPException(status_code=502, detail=str(e))
    return {"instance_id": instance_id}


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


@app.post("/agent/instances/{instance_id}/terminate")
def terminate(instance_id: str) -> dict:
    try:
        app.state.runner.terminate_workflow(instance_id)
        return {"instance_id": instance_id, "status": "terminated"}
    except Exception as e:  # noqa: BLE001
        log.warning("terminate %s failed: %s", instance_id, e)
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/agent/instances/{instance_id}/purge")
def purge(instance_id: str) -> dict:
    try:
        app.state.runner.purge_workflow(instance_id)
        return {"instance_id": instance_id, "status": "purged"}
    except Exception as e:  # noqa: BLE001
        log.warning("purge %s failed: %s", instance_id, e)
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}
