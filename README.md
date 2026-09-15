# Bank Creditor Demo

LangGraph agent durability demo, running as durable Dapr Workflows via Diagrid Catalyst. 100 agents credit 10 customer accounts from $100 → $200, $1 at a time, while chaos is continuously being injected.

At the finish of every demo flow run, every account finishes at exactly $200, with a total transaction count of 1000, regardless of pod kills, AZ evictions, dropped MCP calls, or latency injection. Exactly-once credits are enforced at the database level via a composite `(execution_run_id, tx_id)` primary key.

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│ UI (served by MCP)                                                │
│   ▲                                                               │
│   │ HTTP                                                          │
│ ┌─┴────────────────┐          ┌────────────────────────────┐      │
│ │ MCP server       │◄─────────│ Agent worker               │      │
│ │ · orchestrator   │          │ · LangGraph                │      │
│ │ · chaos surface  │          │ · diagrid.agent.langgraph  │      │
│ └─┬────────────────┘          └─┬──────────────────────────┘      │
│   │                             │ schedule-one                    │
│   ▼                             ▼                                 │
│     ┌──────────────┐          ┌────────────────────┐              │
│     │ Postgres     │◄─────────│ Replenisher        │              │
│     │ bankdemo +   │          │ (MCP-side)         │              │
│     │ dapr_state   │          └────────────────────┘              │
│     └──────────────┘                                              │
└──────────────────────────────────────────────────────────────────┘
```

The agent reaches its Postgres-backed tools (`get_balance`, `credit_account`, etc.) through Catalyst's managed MCP proxy. Every deployment path therefore requires a Catalyst project (Self-Hosted or Cloud); see [Deployment](docs/DEPLOYMENT.md) for the options.

## Documentation

- **[Deployment](docs/DEPLOYMENT.md)** — deploy to Kubernetes (any conformant cluster), or run locally against Catalyst
  - [Catalyst BYOC](docs/CATALYST_SELF_HOSTED.md) — Catalyst control plane inside your own cluster
  - [Catalyst local dev](docs/CATALYST.md) — `diagrid dev run` on your laptop
- **[Demo flow](docs/DEMO_FLOW.md)** — presenter script for running the live demo
- **[Troubleshooting](docs/TROUBLESHOOTING.md)** — common failures across all deploy paths
- **[Local Dapr (no Catalyst)](docs/LOCAL_DAPR.md)** — *not a deployment path* — a debugging recipe for testing Dapr Workflow's own durability in isolation from Catalyst's hosted backend

## Databases

| DB | Purpose |
|---|---|
| `bankdemo` | App data: `customers`, `accounts`, `execution_runs`, `transactions`, `audit_log` |
| `dapr_state` | Dapr workflow checkpoints — physically isolated; MCP can't see it |

`pg_notify('tx_committed', ...)` fires on every transaction insert. The MCP server LISTENs on that channel and pushes per-tx events to the UI over WebSocket — that's how customer balances tick smoothly $1 at a time rather than in poll-sized batches.

## Repo layout

```
.
├── ui-prototype/              # React UI (loaded via babel-standalone)
│   ├── index.html
│   ├── styles.css
│   └── src/                   # shell, grids, panels, telemetry, app
├── deploy/
│   ├── postgres/              # Helm chart: Postgres + schema + seed
│   ├── mcp/                   # Helm chart: MCP server (also hosts the UI + Replenisher)
│   ├── agent/                 # Helm chart: agent worker + Dapr Component
│   └── ingress/               # Helm chart: nginx Ingress
├── local/
│   ├── compose.yaml           # docker compose: postgres + mcp (no agent — agent runs locally)
│   └── init.sql               # bankdemo schema + dapr_state DB
├── services/
│   ├── mcp/                   # FastAPI + mcp SDK + asyncpg + orchestrator + replenisher + WS
│   └── agent/                 # LangGraph agent (diagrid.agent.langgraph) + stub LLM
└── docs/
    ├── DEPLOYMENT.md           # Kubernetes + local deployment instructions
    ├── CATALYST.md             # Catalyst local bring-up (`diagrid dev run`)
    ├── CATALYST_SELF_HOSTED.md # Catalyst Self-Hosted in your cluster
    ├── DEMO_FLOW.md            # Presenter script for the live demo
    ├── TROUBLESHOOTING.md      # Common failures across all deploy paths
    └── LOCAL_DAPR.md           # Debug recipe: plain Dapr, no Catalyst (not a deploy path)
```
