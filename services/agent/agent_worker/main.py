import asyncio
import contextlib
import logging
import os

import httpx
from dapr.ext.workflow import DaprWorkflowClient, WorkflowRuntime
from dapr_agents.workflow.runners.agent import AgentRunner
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .agent import build_agent

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("main")

DAPR_HTTP_PORT = os.environ.get("DAPR_HTTP_PORT", "3500")
# `/healthz/outbound` not `/healthz`: the latter requires daprd to have
# discovered our app on port 8000, which we haven't bound yet.
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
    # Default thread pool serializes I/O-bound activities; bump for parallelism.
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
    # Mount the framework's terminate/purge HTTP routes (PR
    # dapr/dapr-agents#438). We pick just the service routes — skipping the
    # runner's subscribe()/HITL wiring, which would add pubsub traffic we
    # don't use.
    runner = AgentRunner()
    runner._mount_service_routes(
        fastapi_app=app_,
        agent=agent,
        entry_path="/agent/run",
        status_path="/agent/instances/{instance_id}",
        workflow_component="dapr",
        fetch_status_payloads=True,
    )
    app_.state.agent = agent
    app_.state.runtime = runtime
    app_.state.agent_runner = runner
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


class ScheduleOneBody(BaseModel):
    instance_id: str = Field(min_length=1)
    prompt: str = Field(default="")


@app.post("/schedule-one")
def schedule_one(body: ScheduleOneBody) -> dict:
    """Stateless workflow scheduler. The MCP-side replenisher posts here for
    each workflow it wants to start; this pod's local Dapr sidecar handles
    the schedule_new_workflow gRPC call. No in-process state — any agent
    replica can serve this and Dapr's placement service routes the workflow
    onto whichever agent ends up hosting it."""
    workflow_name = os.environ.get(
        "FORCE_WORKFLOW_NAME", "dapr.agents.banker.workflow"
    )

    def _wf_proxy():  # noqa: D401
        pass

    _wf_proxy.__name__ = workflow_name
    wf_client = DaprWorkflowClient()
    try:
        wf_client.schedule_new_workflow(
            workflow=_wf_proxy,
            input={"task": body.prompt},
            instance_id=body.instance_id,
        )
        return {"ok": True, "instance_id": body.instance_id}
    except Exception as e:  # noqa: BLE001
        log.warning("schedule %s failed: %s", body.instance_id, e)
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/trigger")
def trigger(body: TriggerBody) -> dict:
    instance_id = f"customer-{body.customer_id}"
    prompt = (
        f"Drain customer {body.customer_id}'s account up to ${body.target}. "
        f"Use $1 credits."
    )
    # dapr-agents 1.x registers as `dapr.agents.<name-lower>.workflow`.
    workflow_name = os.environ.get(
        "FORCE_WORKFLOW_NAME", "dapr.agents.banker.workflow"
    )

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
