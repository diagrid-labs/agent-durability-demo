"""FastAPI wrapper — same route surface as services/agent/agent_worker/main.py
so services/mcp/mcp_server/replenisher.py works against this agent unchanged.
It only ever POSTs /schedule-one and /agent/instances/{id}/terminate over
HTTP; it has no idea (and doesn't need to) that there's no Dapr underneath.

The durability story is deliberately absent: `_tasks` is a plain in-memory
dict. If this process dies, every task in it — and whatever mid-credit
progress that account had — is just gone. No other replica can see it, let
alone resume it. That's the whole point of this deployment; see agent.py's
module docstring.
"""

import asyncio
import logging
import os

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .agent import GRAPH

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("main")

app = FastAPI()

_tasks: dict[str, asyncio.Task] = {}


class TriggerBody(BaseModel):
    customer_id: int = Field(ge=1)
    target: int = Field(default=200, ge=1, le=10_000)


class ScheduleOneBody(BaseModel):
    instance_id: str = Field(min_length=1)
    customer_id: int = Field(ge=1)


def _schedule(instance_id: str, customer_id: int) -> None:
    initial_state = {
        "requester": instance_id,
        "customer_id": customer_id,
        "last_result": None,
        "done": False,
        "final_message": None,
    }
    task = asyncio.create_task(
        GRAPH.ainvoke(
            initial_state, config={"configurable": {"thread_id": instance_id}}
        )
    )
    _tasks[instance_id] = task


@app.post("/schedule-one")
async def schedule_one(body: ScheduleOneBody) -> dict:
    try:
        _schedule(body.instance_id, body.customer_id)
        return {"ok": True, "instance_id": body.instance_id}
    except Exception as e:  # noqa: BLE001
        log.warning("schedule %s failed: %s", body.instance_id, e)
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/trigger")
async def trigger(body: TriggerBody) -> dict:
    instance_id = f"customer-{body.customer_id}"
    try:
        _schedule(instance_id, body.customer_id)
    except Exception as e:  # noqa: BLE001
        log.warning("trigger %s failed: %s", instance_id, e)
        raise HTTPException(status_code=502, detail=str(e))
    return {"instance_id": instance_id}


@app.get("/status/{instance_id}")
def status(instance_id: str) -> dict:
    task = _tasks.get(instance_id)
    if task is None:
        raise HTTPException(404, f"instance {instance_id} not found")
    if task.cancelled():
        runtime_status = "TERMINATED"
    elif not task.done():
        runtime_status = "RUNNING"
    elif task.exception() is not None:
        runtime_status = "FAILED"
    else:
        runtime_status = "COMPLETED"
    failure = None
    if runtime_status == "FAILED":
        exc = task.exception()
        failure = {
            "error_type": type(exc).__name__,
            "message": str(exc),
            "stack_trace": None,
        }
    return {
        "instance_id": instance_id,
        "runtime_status": runtime_status,
        "serialized_output": (
            str(task.result()) if runtime_status == "COMPLETED" else None
        ),
        "failure": failure,
    }


@app.post("/agent/instances/{instance_id}/terminate")
def terminate(instance_id: str) -> dict:
    task = _tasks.get(instance_id)
    if task is None:
        raise HTTPException(404, f"instance {instance_id} not found")
    task.cancel()
    return {"instance_id": instance_id, "status": "terminated"}


@app.post("/agent/instances/{instance_id}/purge")
def purge(instance_id: str) -> dict:
    _tasks.pop(instance_id, None)
    return {"instance_id": instance_id, "status": "purged"}


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}
