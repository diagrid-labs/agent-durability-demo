# Bank Heist Demo — Implementation Plan

Dapr Agents durability demo: 100 agents drain 10 customer accounts from $100 → $200, $1 at a time, while chaos is injected. Invariant: every account finishes at exactly $200, total transaction count = 1000, regardless of failures.

## Locked constraints

- **100 agents** — hard requirement (matches UI heatmap).
- **LLM stubbed by default**, `--real-model` flag enables a live model. May flip to always-on later.
- **Runs on Kubernetes** (not bare processes / docker-compose).
- **Stack**: Python (FastAPI + dapr-agents + mcp SDK), Postgres, React UI (already built).

## Architecture

```
React UI ──WS─►  Orchestrator (FastAPI)
                    │
                    ├── /api/chaos/*   ──► Chaos controller (kubectl / k8s API)
                    ├── /api/control/* ──► Workflow lifecycle (Dapr API)
                    └── /ws/telemetry  ◄── Event aggregator (DB CDC + pod watch)

Agent worker pods (Dapr Agents) ──MCP──► MCP server ──► Postgres
                ▲
                └── Dapr workflow state store (Postgres, separate schema)
```

### Components

1. **Postgres** — application data (customers, accounts, transactions, audit_log) AND Dapr workflow state store (separate schema). Single instance, lives in a non-chaos zone.
2. **MCP server** (Python, `mcp` SDK). Sole gateway to Postgres for agents. Tools:
   - `list_customers()` → all 10
   - `get_customer(id)` → enriched profile (name, tier, risk)
   - `credit_account(customer_id, amount, tx_id)` → idempotent on `tx_id`
   - `get_balance(customer_id)`
3. **Dapr Agents workers** — Python pods running `dapr-agents`. Each worker hosts multiple workflow instances.
4. **Orchestrator** (FastAPI) — spawns workflows, exposes chaos endpoints, streams telemetry over WS.
5. **Chaos controller** — uses k8s API + chaos-mesh (or raw kubectl) to apply faults.
6. **React UI** — already built. Replace `createTelemetry` body with a WS client; component contract unchanged.

## Kubernetes topology — multi-node, 3 zones

**Recommendation: 3-node kind cluster, zones `a`/`b`/`c`, with a separate "control" plane that is never attacked.**

| Resource | Placement | Notes |
|---|---|---|
| Postgres | control zone | StatefulSet, 1 replica |
| Dapr placement | control zone | 3 replicas for HA later, 1 fine for demo |
| MCP server | control zone | Deployment, 2 replicas |
| Orchestrator + UI | control zone | Deployment, 1 replica |
| Agent workers | spread across a/b/c | `topologySpreadConstraints` maxSkew=1 |

Node labels: `topology.kubernetes.io/zone={a,b,c,control}`.

### Why multi-node

Single-node rejected: AZ outage cannot be demonstrated with real k8s primitives, only via a flag toggle, which undercuts the entire durability narrative. Multi-node cost: ~3x resources, +2min setup, still laptop-runnable on kind.

### 100 agents → pods

Two options:

**Option A (recommended): 100 workflow instances on ~15 worker pods (5 per chaos zone).**
- Each cell in the UI = one workflow instance ID.
- Workers host 5–10 instances each.
- "Kill agent N" → Dapr API to terminate that workflow instance, OR kill the pod currently hosting it; surviving worker picks it up.
- Cheaper, identical visual.

**Option B: 100 pods, 1 workflow per pod (~33 per zone).**
- More visceral (each heatmap cell = a real pod).
- Heavy on laptop (100 pods + 100 Dapr sidecars = 200 containers).
- Use only if Option A's "logical agent" abstraction feels weaker in the demo.

Default to Option A; expose a Helm values toggle to switch.

## Database schema

```sql
CREATE TABLE customers (
  id          INT PRIMARY KEY,
  name        TEXT NOT NULL,
  tier        TEXT,
  risk        TEXT
);

CREATE TABLE accounts (
  customer_id INT PRIMARY KEY REFERENCES customers(id),
  balance     NUMERIC(10,2) NOT NULL DEFAULT 100.00,
  target      NUMERIC(10,2) NOT NULL DEFAULT 200.00,
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE transactions (
  tx_id       TEXT PRIMARY KEY,             -- idempotency key
  customer_id INT NOT NULL REFERENCES customers(id),
  amount      NUMERIC(10,2) NOT NULL,
  agent_id    TEXT NOT NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE audit_log (
  id         BIGSERIAL PRIMARY KEY,
  ts         TIMESTAMPTZ NOT NULL DEFAULT now(),
  event_type TEXT NOT NULL,                 -- mcp.req | mcp.res | chaos | system
  payload    JSONB NOT NULL
);
```

`tx_id` format: `wf-{workflow_instance_id}-step-{n}`. Deterministic — workflow replay produces the same key, so retries collapse via the PK.

### Idempotent credit (the heart of the demo)

```sql
BEGIN;
INSERT INTO transactions (tx_id, customer_id, amount, agent_id)
VALUES ($1, $2, $3, $4)
ON CONFLICT (tx_id) DO NOTHING
RETURNING tx_id;
-- Only update balance if we actually inserted (i.e. first time)
UPDATE accounts
SET balance = LEAST(target, balance + $3),
    updated_at = now()
WHERE customer_id = $2 AND EXISTS (
  SELECT 1 FROM transactions WHERE tx_id = $1
);
COMMIT;
```

This makes `credit_account` exactly-once at the database, regardless of how many times Dapr replays the workflow step.

## Demo invariant (the proof)

At demo end:
- `SELECT SUM(balance) FROM accounts` = `customers * 200` = `2000.00`
- `SELECT COUNT(*) FROM transactions` = `(target - start) * customers` = `1000`
- Holds even if `kubectl delete pod` is run continuously throughout.

UI displays both counters live. The "no transactions lost" claim is verifiable, not asserted.

## Chaos → k8s mechanism mapping

| UI button | k8s primitive |
|---|---|
| `killRandom(N)` | `kubectl delete pod` on N agent worker pods (random) |
| `killAZ()` | `kubectl cordon` zone-b + delete all worker pods labeled `zone=b`; uncordon after 5s |
| `pauseFleet(ms)` | chaos-mesh `PodChaos` with action `pod-failure` for the window, OR set readiness probe to fail (effectively pauses) |
| `latencyJitter(ms)` | chaos-mesh `NetworkChaos` `delay` 200ms ± 100ms on agent → MCP traffic; OR feature flag in MCP server adding sleep |
| `dropTx()` | feature flag in MCP server: next call returns 503; workflow retries, idempotency key wins |
| `fireAll()` | sequence of all of the above, staggered |

Start with the MCP-server-flag implementations (no chaos-mesh dependency). Layer chaos-mesh in once the basic story works.

## LLM stubbing

`dapr-agents` agents normally invoke an LLM per step. For this demo:
- Default mode: `STUB_LLM=true` — agent's "decide which customer to credit" step calls a deterministic Python function (pick lowest-balance not-done customer with random tiebreak) instead of a model.
- Real-model mode: `STUB_LLM=false` (and `ANTHROPIC_API_KEY` present) — agent uses a real model with a tool definition for `credit_account`. Slower, costs money, but shows the LLM-tool-calling story.
- Helm value: `agent.llm.mode = stub | real`.

## UI wiring

`ui-prototype/src/telemetry.jsx:createTelemetry` is currently a self-contained simulator. Replacement:

```js
function createTelemetry(initial) {
  const ws = new WebSocket(WS_URL);
  const state = { /* same shape as today */ };
  const listeners = new Set();
  ws.onmessage = (m) => { Object.assign(state, JSON.parse(m.data)); listeners.forEach(fn => fn(state)); };
  const control = {
    killRandom: (n) => fetch('/api/chaos/kill-random', { method: 'POST', body: JSON.stringify({ n }) }),
    killAZ:     ()  => fetch('/api/chaos/kill-az',     { method: 'POST' }),
    // ... etc, one per UI button
  };
  return { get state() { return state; }, subscribe(fn) { listeners.add(fn); return () => listeners.delete(fn); }, control };
}
```

**No component changes.** `Heatmap`, `AgentsGrid`, `CustomersGrid`, `ChaosPanel`, `CustodyPanel`, MCP log all keep their props.

### Telemetry event sources (server-side)

The orchestrator aggregates from:
- **Postgres LISTEN/NOTIFY** on `transactions` insert (or logical replication) → updates `customers[].balance`, `counters.txProcessed`.
- **Dapr workflow events** (or polling workflow status API) → updates `agents[].status`.
- **k8s pod watch** (informer) → maps pods to agent IDs for status (`alive`/`restarting`/`dead`).
- **MCP server hook** → emits `mcp.req`/`mcp.res` lines.

Pushed to the UI as a single merged state diff every ~250ms (not every event — debounced).

## Build order

1. **Postgres + schema + seed** — Helm chart, 10 customers, $100 each.
2. **MCP server** with idempotent `credit_account`. Standalone test: hammer it concurrently from 100 goroutines / asyncio tasks, assert COUNT(transactions) is correct and no double-credit.
3. **One Dapr Agent workflow** that drains one customer to $200. Test durability: `kill -9` mid-run, watch it resume exactly where it left off, balance arrives at $200, transactions table has no duplicates.
4. **Orchestrator + WS feed** — stub events first, just to wire UI.
5. **Swap UI's `createTelemetry`** for the WS client. Verify all panels render live data.
6. **Spawn 100 workflow instances**, watch the heatmap fill in.
7. **Chaos endpoints**, one button at a time. Validate invariant after each.
8. **`fireAll` end-to-end** — full chaos sequence, all 10 accounts finish at $200, transaction count = 1000.
9. **Real-LLM mode** — flag, prompt, tool definition, retest invariant.
10. **Polish** — Helm chart, README, demo script.

## Open questions / risks

- **Workflow state store sizing**: 100 instances × ~100 steps × workflow history = bounded, but verify Postgres isn't the bottleneck under chaos. May need to tune Dapr's actor reminder partitioning.
- **chaos-mesh vs raw kubectl**: chaos-mesh is more powerful (network chaos, real partitions) but adds a CRD dependency. Plan starts with kubectl + MCP-server flags, layers chaos-mesh in step 7+.
- **Dapr placement service HA**: single replica fine for demo; if killed, all workflows pause. Document this; do not target placement in chaos buttons.
- **WS reconnection**: UI must reconnect on transient disconnects (WebSocket close during pod restart). Add backoff in the replacement `createTelemetry`.
- **Real-model determinism**: with a real LLM, the agent might pick different customers each run, producing different tx orderings. Invariant still holds (commutative additions), but the demo's reproducibility narrows.

## Decisions deferred

- chaos-mesh adoption (step 7 decision point)
- 1-pod-per-agent vs pool (Helm toggle, default pool)
- Multi-region (out of scope for v1; AZ-level is enough story)
