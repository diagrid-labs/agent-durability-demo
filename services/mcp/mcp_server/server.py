import contextlib
import logging
import os
from typing import Any

from fastapi import FastAPI, HTTPException
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field

from .chaos import Chaos, DroppedCallError
from .db import Database
from .orchestrator import Orchestrator

logger = logging.getLogger("mcp_server")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

db = Database()
chaos = Chaos()
orch = Orchestrator()
mcp = FastMCP(
    "bank-heist-postgres",
    streamable_http_path="/",
    # Stateless: every request is independent, no session ID required. Lets us
    # run multiple MCP server replicas without session affinity in the Service.
    stateless_http=True,
    # FastMCP rejects unknown Host headers by default (DNS rebinding protection).
    # Allow the in-cluster Service DNS so agent pods can reach us via k8s DNS.
    transport_security=TransportSecuritySettings(
        allowed_hosts=[
            "mcp.bank-heist.svc.cluster.local",
            "mcp.bank-heist.svc.cluster.local:8000",
            "mcp",
            "mcp:8000",
            "localhost",
            "localhost:8000",
            "localhost:9000",
            "127.0.0.1",
            "127.0.0.1:8000",
            "127.0.0.1:9000",
        ],
    ),
)


@mcp.tool()
async def list_customers() -> list[dict[str, Any]]:
    """List all customers with their tier, risk, current balance, and target."""
    await chaos.maybe_delay()
    chaos.maybe_drop()
    return await db.list_customers()


@mcp.tool()
async def get_customer(customer_id: int) -> dict[str, Any]:
    """Return a single customer's enriched profile."""
    await chaos.maybe_delay()
    chaos.maybe_drop()
    row = await db.get_customer(customer_id)
    if row is None:
        raise ValueError(f"customer {customer_id} not found")
    return row


@mcp.tool()
async def get_balance(customer_id: int) -> dict[str, Any]:
    """Return the current balance and target for a customer."""
    await chaos.maybe_delay()
    row = await db.get_balance(customer_id)
    if row is None:
        raise ValueError(f"customer {customer_id} not found")
    return row


@mcp.tool()
async def credit_account(
    customer_id: int,
    amount: float,
    tx_id: str,
    agent_id: str,
) -> dict[str, Any]:
    """Idempotently credit `amount` to `customer_id`. `tx_id` must be deterministic
    across workflow replays (e.g. `wf-{instance_id}-step-{n}`); duplicate tx_ids
    are silently absorbed and return the existing balance with `applied=false`."""
    await chaos.maybe_delay()
    chaos.maybe_drop()
    return await db.credit_account(customer_id, amount, tx_id, agent_id)


@mcp.tool()
async def get_next_task(requester: str = "") -> dict[str, Any]:
    """Ask the orchestrator for the next pending credit task.

    Pass `requester` (your stable workflow identity, e.g. 'slot-7-task-3-r123')
    so replays return the same task instead of popping a new one. Returns
    either {done: true} when the queue is drained, or
    {done: false, customer_id, tx_id, target, n}."""
    return await orch.next_task(requester=requester or None)


@mcp.tool()
async def report_done(tx_id: str, applied: bool) -> dict[str, Any]:
    """Notify the orchestrator that the task identified by tx_id is complete.
    `applied=true` if a credit_account call succeeded, `applied=false` if the
    customer was already at target."""
    return await orch.report_done(tx_id, applied)


@mcp.tool()
async def process_task(requester: str = "") -> dict[str, Any]:
    """Atomically claim + balance-check + (credit if needed) + report a single
    task. Equivalent to GetNextTask → GetBalance → CreditAccount? → ReportDone
    rolled into one call. Cuts the workflow's activity count from ~17 to ~7,
    which matters when the Catalyst worker is far from a managed-workflow
    region.

    Pass `requester` (your stable workflow identity) so replays return the
    same task. Returns {done: true} when the queue is drained, otherwise
    {done: false, tx_id, customer_id, applied, balance_before, target}."""
    task = await orch.next_task(requester=requester or None)
    if task.get("done"):
        return {"done": True}

    customer_id = int(task["customer_id"])
    tx_id = str(task["tx_id"])
    target = float(task["target"])

    applied = False
    balance: float | None = None
    error: str | None = None
    try:
        # Mirrors the chaos points that GetBalance / CreditAccount apply in
        # multi-tool mode, so mode=single sees the same fault profile.
        await chaos.maybe_delay()
        balance_row = await db.get_balance(customer_id)
        if balance_row is None:
            error = f"customer {customer_id} not found"
        else:
            balance = float(balance_row["balance"])
            if balance < target:
                await chaos.maybe_delay()
                chaos.maybe_drop()
                credit_result = await db.credit_account(
                    customer_id, 1, tx_id, "banker"
                )
                applied = bool(credit_result.get("applied", True))
    except Exception as e:  # noqa: BLE001
        # Without this, an MCP/DB hiccup leaks the in_flight slot forever
        # because durabletask's default activity retry policy is max_attempts=1.
        error = str(e)
    finally:
        # Always release the orchestrator's in_flight lock so the queue can
        # heal even when this activity ends up failing the workflow.
        await orch.report_done(tx_id, applied)

    return {
        "done": False,
        "tx_id": tx_id,
        "customer_id": customer_id,
        "applied": applied,
        "balance_before": balance,
        "target": target,
        "error": error,
    }


# --- Chaos control surface (orchestrator-only; restrict via NetworkPolicy later) ---

class DropBody(BaseModel):
    count: int = Field(default=1, ge=1, le=100)


class LatencyBody(BaseModel):
    ms: int = Field(ge=0, le=10_000)
    duration_ms: int = Field(default=4_000, ge=0, le=60_000)


def build_app() -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        await db.connect()
        async with mcp.session_manager.run():
            yield
        await db.close()

    app = FastAPI(lifespan=lifespan)
    app.mount("/mcp", mcp.streamable_http_app())

    @app.post("/chaos/drop")
    async def chaos_drop(body: DropBody) -> dict[str, Any]:
        chaos.arm_drop(body.count)
        return chaos.snapshot()

    @app.post("/chaos/latency")
    async def chaos_latency(body: LatencyBody) -> dict[str, Any]:
        chaos.arm_latency(body.ms, body.duration_ms)
        return chaos.snapshot()

    @app.post("/chaos/reset")
    async def chaos_reset() -> dict[str, Any]:
        chaos.reset()
        return chaos.snapshot()

    @app.get("/chaos")
    async def chaos_state() -> dict[str, Any]:
        return chaos.snapshot()

    class OrchResetBody(BaseModel):
        customers: int = Field(default=10, ge=1, le=100)
        credits_per_customer: int = Field(default=100, ge=1, le=1000)
        target: int = Field(default=200, ge=1, le=10_000)

    @app.post("/orch/reset")
    async def orch_reset(body: OrchResetBody) -> dict[str, Any]:
        return await orch.reset(
            customers=body.customers,
            credits_per_customer=body.credits_per_customer,
            target=body.target,
        )

    @app.get("/orch/status")
    async def orch_status() -> dict[str, Any]:
        return await orch.status()

    class OrchSweepBody(BaseModel):
        timeout_seconds: float = Field(default=30.0, ge=1.0, le=600.0)

    @app.post("/orch/sweep")
    async def orch_sweep(body: OrchSweepBody) -> dict[str, Any]:
        return await orch.sweep(timeout_seconds=body.timeout_seconds)

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        # Cheap liveness — DB connectivity proven by the pool's existence.
        try:
            async with db.pool.acquire() as conn:
                await conn.execute("SELECT 1")
            return {"status": "ok"}
        except Exception as e:  # noqa: BLE001
            raise HTTPException(503, str(e))

    # Convert simulated drops to a 5xx so MCP propagates a tool error to the
    # caller, which Dapr workflow surfaces as a step failure → automatic retry.
    @app.exception_handler(DroppedCallError)
    async def _drop_handler(_, exc: DroppedCallError):  # type: ignore[no-untyped-def]
        raise HTTPException(503, str(exc))

    return app


app = build_app()
