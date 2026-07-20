import asyncio
import contextlib
import json
import logging
import os
import re
import time
from collections import deque
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field

from .chaos import Chaos, DroppedCallError
from .db import Database
from .orchestrator import Orchestrator
from .pod_chaos import PodChaosController
from .replenisher import Replenisher
from .slot_tracker import SlotTracker

logger = logging.getLogger("mcp_server")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

db = Database()
chaos = Chaos()
orch = Orchestrator()
pod_chaos = PodChaosController()
replenisher = Replenisher(orch)
slots = SlotTracker()


class TxBroadcaster:
    """Fan-out of `tx_committed` notifications to connected WebSocket clients.

    A single asyncpg LISTEN connection feeds an asyncio.Queue (see
    Database.listen_transactions); the broadcast loop drains the queue and
    pushes each event to every registered client. Slow/dead clients are
    dropped silently — the demo prefers freshness over delivery guarantees."""

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def add(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._clients.add(ws)

    async def remove(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)

    async def broadcast(self, message: dict[str, Any]) -> None:
        if not self._clients:
            return
        text = json.dumps(message, default=str)
        async with self._lock:
            clients = list(self._clients)
        dead: list[WebSocket] = []
        for ws in clients:
            try:
                await ws.send_text(text)
            except Exception:  # noqa: BLE001
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._clients.discard(ws)


broadcaster = TxBroadcaster()
_tx_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=10_000)
_tx_stop = asyncio.Event()

# In-memory ring buffer feeding the UI's MCP-server card. Server-monotonic
# `ts` so the UI's fmtClock renders mm:ss since server start.
MCP_LOG: deque[dict[str, Any]] = deque(maxlen=60)
_SERVER_STARTED = time.monotonic()
_MCP_SEQ = 0
_MCP_QUERIES = 0


def _ts_ms() -> float:
    return (time.monotonic() - _SERVER_STARTED) * 1000.0


def log_mcp(kind: str, text: str) -> None:
    global _MCP_SEQ, _MCP_QUERIES
    _MCP_SEQ += 1
    if kind == "req":
        _MCP_QUERIES += 1
    MCP_LOG.append({"id": _MCP_SEQ, "kind": kind, "text": text, "ts": _ts_ms()})


async def _drain_tx_queue() -> None:
    """Pump `tx_committed` payloads from the listener queue out to all
    connected WebSocket clients. Each payload is the JSON string the
    postgres trigger built — we forward as-is under `{"type": "tx", ...}`
    so the UI can route on type."""
    while True:
        payload = await _tx_queue.get()
        try:
            data = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            continue
        data["type"] = "tx"
        await broadcaster.broadcast(data)


class _MCPTrailingSlashMiddleware:
    """Catalyst's MCP proxy relays a caller's actual tool-call requests to the
    registered upstream URL with the trailing slash stripped (its own health
    ping keeps the slash). Bare `/mcp` only gets a partial match against the
    `/mcp` Mount below, so Starlette falls through to the `/` StaticFiles
    catch-all, which rejects POST with 405 before FastMCP ever sees it.
    Normalize the path here, ahead of routing."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"] == "/mcp":
            scope = dict(scope, path="/mcp/")
        await self.app(scope, receive, send)


log_mcp("sys", "CONNECT mcp://postgres.bank.svc · session opened")
mcp = FastMCP(
    "bank-creditor-postgres",
    streamable_http_path="/",
    # Stateless: every request is independent, no session ID required. Lets us
    # run multiple MCP server replicas without session affinity in the Service.
    stateless_http=True,
    # FastMCP rejects unknown Host headers by default (DNS rebinding protection).
    # Allow the in-cluster Service DNS so agent pods can reach us via k8s DNS.
    transport_security=TransportSecuritySettings(
        allowed_hosts=[
            "mcp.bank-creditor.svc.cluster.local",
            "mcp.bank-creditor.svc.cluster.local:80",
            "mcp.bank-creditor.svc.cluster.local:8000",
            "mcp",
            "mcp:80",
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
    log_mcp("req", "SELECT id,name,tier,risk,balance,target FROM customers JOIN accounts")
    await chaos.maybe_delay()
    chaos.maybe_drop()
    rows = await db.list_customers()
    log_mcp("res", f"{len(rows)} rows")
    return rows


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
    execution_run_id: int,
) -> dict[str, Any]:
    """Idempotently credit `amount` to `customer_id` within `execution_run_id`.
    `tx_id` must be deterministic across workflow replays (e.g.
    `wf-{instance_id}-step-{n}`); the (execution_run_id, tx_id) pair is the
    composite idempotency key — duplicates are absorbed and return the
    existing balance with `applied=false`. Always pass through the
    execution_run_id you received from get_next_task — never invent one."""
    log_mcp(
        "req",
        f"credit_account run={execution_run_id} cust={customer_id} +${amount} tx={tx_id}",
    )
    await chaos.maybe_delay()
    chaos.maybe_drop()
    result = await db.credit_account(
        customer_id, amount, tx_id, agent_id, execution_run_id
    )
    tag = "applied" if result.get("applied") else "duplicate"
    log_mcp("res", f"{tag} · cust={customer_id} balance=${result.get('balance')}")
    return result


@mcp.tool()
async def get_next_task(requester: str = "", pod: str = "") -> dict[str, Any]:
    """Ask the orchestrator for the next pending credit task.

    Pass `requester` (your stable workflow identity, e.g. 'slot-7-task-3-r123')
    so replays return the same task instead of popping a new one. Returns
    either {done: true} when the queue is drained, or
    {done: false, customer_id, tx_id, target, n}.

    `pod` is the agent pod hostname; recorded so the heatmap pod-fleet view
    can map slots to pods."""
    agent_slot = _slot_from_requester(requester)
    if agent_slot is not None and pod:
        await slots.record(agent_slot, pod)
    return await orch.next_task(requester=requester or None)


@mcp.tool()
async def report_done(tx_id: str, applied: bool) -> dict[str, Any]:
    """Notify the orchestrator that the task identified by tx_id is complete.
    `applied=true` if a credit_account call succeeded, `applied=false` if the
    customer was already at target."""
    return await orch.report_done(tx_id, applied)


_SLOT_FROM_REQUESTER = re.compile(r"^agent-(\d+)-task-")


def _slot_from_requester(requester: str | None) -> int | None:
    """Extract heatmap slot N from `agent-NNN-task-K-r<run>` requester IDs.
    Returns None for requester formats that don't carry a slot (legacy
    callers, ad-hoc test scripts)."""
    if not requester:
        return None
    m = _SLOT_FROM_REQUESTER.match(requester)
    return int(m.group(1)) if m else None


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
        await orch.bootstrap(db)
        # Start the listener (dedicated asyncpg conn) and the broadcaster
        # drain loop. Both run for the lifetime of the process.
        listener_task = asyncio.create_task(
            db.listen_transactions(_tx_queue, _tx_stop), name="pg-listener"
        )
        broadcast_task = asyncio.create_task(
            _drain_tx_queue(), name="ws-broadcast"
        )
        async with mcp.session_manager.run():
            yield
        _tx_stop.set()
        for t in (listener_task, broadcast_task):
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        await db.close()

    app = FastAPI(lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(_MCPTrailingSlashMiddleware)
    app.mount("/mcp", mcp.streamable_http_app())

    @app.post("/chaos/drop")
    async def chaos_drop(body: DropBody) -> dict[str, Any]:
        chaos.arm_drop(body.count)
        log_mcp("chaos", f"chaos armed: drop next {body.count} MCP call(s) with 5xx")
        return chaos.snapshot()

    @app.post("/chaos/latency")
    async def chaos_latency(body: LatencyBody) -> dict[str, Any]:
        chaos.arm_latency(body.ms, body.duration_ms)
        log_mcp(
            "chaos",
            f"chaos armed: MCP latency +{body.ms}ms for {body.duration_ms}ms",
        )
        return chaos.snapshot()

    @app.post("/chaos/reset")
    async def chaos_reset() -> dict[str, Any]:
        chaos.reset()
        return chaos.snapshot()

    @app.get("/chaos")
    async def chaos_state() -> dict[str, Any]:
        snap = chaos.snapshot()
        snap["pod_chaos"] = pod_chaos.snapshot()
        return snap

    class PodKillBody(BaseModel):
        # Either pick a specific pod (so UI label = reality) or let the
        # server pick `count` random survivors.
        pod: str = Field(default="")
        count: int = Field(default=1, ge=1, le=100)

    @app.post("/chaos/pod-kill")
    async def chaos_pod_kill(body: PodKillBody) -> dict[str, Any]:
        if body.pod:
            result = pod_chaos.kill_named(body.pod)
        else:
            result = pod_chaos.kill_random(body.count)
        killed_pods: list[str] = result.get("killed", []) or []
        affected_slots: list[int] = []
        for pname in killed_pods:
            affected_slots.extend(await slots.slots_for_pod(pname))
        affected_slots = sorted(set(affected_slots))
        if killed_pods:
            log_mcp(
                "chaos",
                f"pod-kill: deleted {len(killed_pods)} pods · "
                f"{', '.join(killed_pods)} · {len(affected_slots)} slots affected",
            )
            # Reclaim slots immediately so the replenisher backfills fast.
            sweep = await orch.sweep(timeout_seconds=0.0)
            result["released_after_kill"] = sweep.get("released")
            if affected_slots:
                await broadcaster.broadcast({
                    "type": "slot-state",
                    "slots": affected_slots,
                    "status": "dead",
                })
        result["affected_slots"] = affected_slots
        return result

    @app.get("/chaos/pods")
    async def chaos_pods() -> dict[str, Any]:
        """List live agent pods with the current workflow count each is
        servicing (per the slot tracker). UI consumes this to render
        accurate `Kill 1 pod (~N agents)` labels and to pick a victim."""
        live = pod_chaos.list_live_pods()
        counts = await slots.pod_counts()
        for entry in live:
            entry["workflow_count"] = counts.get(entry["pod"], 0)
        return {"pods": live, "available": pod_chaos.snapshot()["available"]}

    # Brief zone-impact pulse so the UI can highlight the affected nodepool.
    _AZ_IMPACT_TTL = 6.0  # seconds
    _az_impacts: dict[str, float] = {}

    def _recently_impacted_zones() -> dict[str, dict[str, Any]]:
        now = time.monotonic()
        return {
            z: {"impacted_until_s": until, "remaining_s": max(0, until - now)}
            for z, until in _az_impacts.items()
            if until > now
        }

    class AzKillBody(BaseModel):
        zone: str = Field(default="")

    @app.post("/chaos/az-kill")
    async def chaos_az_kill(body: AzKillBody) -> dict[str, Any]:
        result = pod_chaos.kill_zone(body.zone or None)
        killed_pods: list[str] = result.get("killed", []) or []
        affected_slots: list[int] = []
        for pname in killed_pods:
            affected_slots.extend(await slots.slots_for_pod(pname))
        affected_slots = sorted(set(affected_slots))
        zone = result.get("zone")
        if killed_pods:
            log_mcp(
                "chaos",
                f"az-kill zone={zone}: deleted {len(killed_pods)} pods · "
                f"{len(affected_slots)} slots affected",
            )
            sweep = await orch.sweep(timeout_seconds=0.0)
            result["released_after_kill"] = sweep.get("released")
            if affected_slots:
                await broadcaster.broadcast({
                    "type": "slot-state",
                    "slots": affected_slots,
                    "status": "dead",
                })
            if zone:
                _az_impacts[zone] = time.monotonic() + _AZ_IMPACT_TTL
                await broadcaster.broadcast({
                    "type": "zone-state",
                    "zone": zone,
                    "status": "impacted",
                    "ttl_seconds": _AZ_IMPACT_TTL,
                })
        result["affected_slots"] = affected_slots
        return result

    @app.get("/chaos/infra")
    async def chaos_infra() -> dict[str, Any]:
        """Operating environment: AKS nodepools and their nodes. The UI
        renders this alongside the pod list so the audience sees the full
        platform context (nodepool split, AZ spread, ready state).
        `impacted_zones` tags zones whose pods were just AZ-killed; the UI
        pulses those nodes/nodepool entries until the TTL expires."""
        nodes = pod_chaos.list_nodes()
        return {
            "available": pod_chaos.snapshot()["available"],
            "nodepools": pod_chaos.list_nodepools(nodes),
            "nodes": nodes,
            "impacted_zones": _recently_impacted_zones(),
        }

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
        snap = await orch.status()
        snap["mcp_queries"] = _MCP_QUERIES
        snap["server_clock_ms"] = _ts_ms()
        return snap

    @app.get("/orch/customers")
    async def orch_customers() -> list[dict[str, Any]]:
        return await db.list_customers()

    @app.get("/orch/mcp-log")
    async def orch_mcp_log() -> dict[str, Any]:
        return {"lines": list(MCP_LOG), "queries": _MCP_QUERIES}

    @app.post("/orch/mcp-log/clear")
    async def orch_mcp_log_clear() -> dict[str, Any]:
        global _MCP_QUERIES
        MCP_LOG.clear()
        _MCP_QUERIES = 0
        return {"cleared": True}

    @app.websocket("/ws/telemetry")
    async def telemetry_ws(ws: WebSocket) -> None:
        """Push-based telemetry: every `tx_committed` notification arrives as
        a `{"type":"tx", execution_run_id, tx_id, customer_id, amount,
        agent_id, created_at}` frame. UI updates balances per-tx instead of
        waiting for the next poll cycle."""
        await broadcaster.add(ws)
        try:
            while True:
                # We don't expect client-to-server messages, but `receive_text`
                # keeps the connection alive and surfaces disconnects.
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        except Exception:  # noqa: BLE001
            pass
        finally:
            await broadcaster.remove(ws)

    # Replenisher runs here (singleton); /schedule-one on the agent is stateless.
    class AgentSpawnBody(BaseModel):
        agents: int = Field(default=100, ge=1, le=500)
        customers: int = Field(default=10, ge=1, le=100)
        credits_per_customer: int = Field(default=100, ge=1, le=1000)
        target: int = Field(default=200, ge=1, le=10_000)

    @app.post("/agent/spawn")
    async def agent_spawn(body: AgentSpawnBody) -> dict[str, Any]:
        return await replenisher.start(
            agents=body.agents,
            customers=body.customers,
            credits_per_customer=body.credits_per_customer,
            target=body.target,
        )

    @app.post("/agent/stop")
    async def agent_stop() -> dict[str, Any]:
        return await replenisher.stop()

    @app.get("/agent/status")
    async def agent_status() -> dict[str, Any]:
        return replenisher.status()

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
        log_mcp("chaos", f"MCP call dropped: {exc} · workflow will retry")
        raise HTTPException(503, str(exc))

    # Serve the UI from / when ui-prototype/ is available. Override via UI_PROTOTYPE_DIR.
    ui_env = os.environ.get("UI_PROTOTYPE_DIR")
    ui_dir = Path(ui_env) if ui_env else Path(__file__).resolve().parents[3] / "ui-prototype"
    if ui_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(ui_dir), html=True), name="ui")

    return app


app = build_app()
