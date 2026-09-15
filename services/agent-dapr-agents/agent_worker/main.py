"""FastAPI wrapper around `dapr_agents`' `DurableAgent`/`AgentRunner`. Builds
one agent at startup and reuses it for every customer this pod serves."""

import contextlib
import json
import logging
import os

from dapr_agents import AgentRunner, DurableAgent
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .agent import build_agent

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("main")

_runner: AgentRunner | None = None


@contextlib.asynccontextmanager
async def lifespan(app_: FastAPI):
    global _runner
    # Built right before serve(), not at import time, to keep them tightly paired.
    _runner = AgentRunner()
    agent = build_agent()
    _runner.serve(agent, app=app_)
    app_.state.agent = agent
    log.info("agent started")
    try:
        yield
    finally:
        _runner.shutdown()


app = FastAPI(lifespan=lifespan)


class ScheduleOneBody(BaseModel):
    instance_id: str = Field(min_length=1)
    customer_id: int = Field(ge=1)


class TriggerBody(BaseModel):
    customer_id: int = Field(ge=1)
    target: int = Field(default=200, ge=1, le=10_000)


async def _schedule(instance_id: str, customer_id: int) -> None:
    # `task` becomes the first message; stub_llm.py reads customer_id back out of it.
    payload = {"task": json.dumps({"customer_id": customer_id})}
    await _runner.run(app.state.agent, payload, instance_id=instance_id, wait=False)


@app.post("/schedule-one")
async def schedule_one(body: ScheduleOneBody) -> dict:
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
    state = _runner.workflow_client.get_workflow_state(instance_id=instance_id)
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
