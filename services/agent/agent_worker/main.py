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
# /healthz requires daprd to have discovered our app on port 8000 already.
DAPR_HEALTH_URL = f"http://127.0.0.1:{DAPR_HTTP_PORT}/v1.0/healthz/outbound"
DAPR_READY_TIMEOUT_S = int(os.environ.get("DAPR_READY_TIMEOUT_S", "120"))


async def wait_for_dapr() -> None:
    """Poll daprd's healthz until 200 — otherwise the workflow runtime races
    daprd's gRPC bind and dies after a 10s timeout."""
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
    # Catalyst mode has no local daprd to wait on — only poll in sidecar mode.
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
    """Schedule one graph run under `instance_id`, returning as soon as
    Catalyst confirms acceptance (first `workflow_started` event) rather than
    waiting for the graph to finish. `workflow_id` is what `/status`,
    `/terminate`, and `/purge` key off of — otherwise a random UUID."""
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
    """Stateless workflow scheduler — any agent replica can serve this;
    Dapr's placement service routes the workflow to whichever agent hosts it."""
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
