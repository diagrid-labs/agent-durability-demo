# Bank Heist Demo

Dapr Agents durability demo. 100 agents drain 10 customer accounts from $100 → $200, $1 at a time, while chaos is injected. Invariant: every account finishes at exactly $200, total transaction count = 1000, regardless of failures.

See [PLAN.md](./PLAN.md) for full architecture and design decisions.

## Prerequisites

- Docker Desktop with **≥6 GB RAM** allocated (5 kind nodes + workloads)
- [kind](https://kind.sigs.k8s.io/) v0.20+
- [helm](https://helm.sh/) v3+
- `kubectl`

## Cluster topology

5 nodes: 1 control-plane (k8s system only) + 4 workers labeled with `topology.kubernetes.io/zone`:

| Zone | Workloads |
|---|---|
| `control` | Postgres, Dapr placement, MCP server, orchestrator (never attacked by chaos) |
| `a`, `b`, `c` | Agent worker pods (chaos targets) |

The control-plane node maps host port `8080` → NodePort `30080` so the UI/orchestrator is reachable at `http://localhost:8080` without `kubectl port-forward`.

## Bring up the cluster

```bash
kind create cluster --config deploy/kind/cluster.yaml
kubectl wait --for=condition=Ready nodes --all --timeout=120s
kubectl get nodes --show-labels | grep zone
```

You should see four worker nodes, one per zone (`control`, `a`, `b`, `c`).

## Deploy Postgres

```bash
helm install demo deploy/postgres --namespace bank-heist --create-namespace
kubectl -n bank-heist rollout status statefulset/postgres
```

### Verify the seed

```bash
kubectl -n bank-heist exec postgres-0 -- \
  psql -U bankadmin -d bankdemo -c "SELECT customer_id, balance FROM accounts ORDER BY customer_id;"
```

Expected: 10 rows, all `100.00`. Customer names match the UI's `nameFor(0..9)` so the heatmap labels stay consistent when the simulator is replaced with the real backend.

### Connect from your laptop

```bash
kubectl -n bank-heist port-forward svc/postgres 5432:5432
PGPASSWORD=bankadmin psql -h localhost -U bankadmin -d bankdemo
```

## Deploy the MCP server

The MCP server is a Python service that fronts Postgres for the agents. It exposes four MCP tools (`list_customers`, `get_customer`, `credit_account`, `get_balance`) plus a `/chaos/*` HTTP surface the orchestrator uses to inject latency and dropped calls.

### Build and load the image into kind

```bash
docker build -t bank-heist/mcp:0.1.0 services/mcp
kind load docker-image bank-heist/mcp:0.1.0 --name bank-heist
```

`kind load` makes the image available to the cluster nodes without needing a registry push.

### Install the chart

```bash
helm install mcp deploy/mcp --namespace bank-heist
kubectl -n bank-heist rollout status deployment/mcp
```

The chart reuses the `postgres-credentials` Secret from the Postgres chart, so deploy Postgres first.

### Smoke test — HTTP surface

```bash
kubectl -n bank-heist port-forward svc/mcp 8000:8000

# Liveness — does a real DB roundtrip
curl -s localhost:8000/healthz

# Inspect chaos state
curl -s localhost:8000/chaos

# Arm a single dropped call (next MCP call returns 503)
curl -s -X POST localhost:8000/chaos/drop -H 'content-type: application/json' -d '{"count":1}'

# Inject 200ms latency for 4s
curl -s -X POST localhost:8000/chaos/latency -H 'content-type: application/json' -d '{"ms":200,"duration_ms":4000}'

# Reset everything
curl -s -X POST localhost:8000/chaos/reset
```

### Smoke test — MCP tool calls

MCP is JSON-RPC over a session-managed streamable HTTP transport, not plain REST, so `curl` can't call the tools directly. Use the official MCP Inspector for interactive testing:

```bash
# port-forward must be running
kubectl -n bank-heist port-forward svc/mcp 8000:8000

npx @modelcontextprotocol/inspector
```

In the Inspector UI:
1. Set transport to **Streamable HTTP**
2. Connect to `http://localhost:8000/mcp`
3. Click **List Tools** → you'll see `list_customers`, `get_customer`, `get_balance`, `credit_account`
4. Pick a tool, fill in args, run it

Idempotency check for `credit_account`: call it twice with the same `tx_id` — the first returns `applied: true` and the new balance; the second returns `applied: false` and the same balance. That's the demo invariant working.

### Rebuilding after code changes

```bash
docker build -t bank-heist/mcp:0.1.0 services/mcp
kind load docker-image bank-heist/mcp:0.1.0 --name bank-heist
kubectl -n bank-heist rollout restart deployment/mcp
```

`kind load` updates the image; `rollout restart` pulls it in (the tag stays the same, so we force a restart instead of relying on a tag bump).

## Install Dapr on the cluster

The agent worker is built on `dapr-agents`, which uses Dapr Workflows underneath. Install the Dapr control plane via the Dapr CLI:

```bash
# One-time: install the Dapr CLI (macOS)
brew install dapr/tap/dapr-cli
# or: curl -fsSL https://raw.githubusercontent.com/dapr/cli/master/install/install.sh | /bin/bash

# Install the Dapr control plane on the kind cluster (kubectl context must point at it)
dapr init -k --wait

# Verify
dapr status -k
```

You should see `dapr-operator`, `dapr-placement-server`, `dapr-sentry`, `dapr-sidecar-injector` healthy. The CLI also gives you `dapr workflow` subcommands later for managing workflow instances.

## Deploy the agent worker (step 3 — single `DurableAgent` durability test)

The agent is a `dapr_agents.DurableAgent` with two tools (`get_balance`, `credit_account`) that wrap the MCP server. By default it runs with a stub LLM (`STUB_LLM=true`) that picks tool calls deterministically — no API key required. Switch to a real Claude model with `--set agent.llm.mode=real --set agent.llm.apiKeySecret=<secret>` (the secret must contain key `ANTHROPIC_API_KEY`).

```bash
docker build -t bank-heist/agent:0.1.0 services/agent
kind load docker-image bank-heist/agent:0.1.0 --name bank-heist
helm install agent deploy/agent --namespace bank-heist
kubectl -n bank-heist rollout status deployment/agent
```

The chart installs:
- A Deployment with the worker container + Dapr sidecar (annotated `dapr.io/enabled: true`)
- A Service exposing `/trigger`, `/status`, `/healthz`
- A Dapr `Component` named `workflowstatestore` pointing at the `dapr_state` Postgres database

### Durability smoke test

```bash
kubectl -n bank-heist port-forward svc/agent 8000:8000

# Start the workflow draining customer 1 to $200
curl -s -X POST localhost:8000/trigger \
  -H 'content-type: application/json' \
  -d '{"customer_id": 1, "target": 200}'
# → {"instance_id": "customer-1", ...}

# Watch the balance climb
watch 'kubectl -n bank-heist exec postgres-0 -- psql -U bankadmin -d bankdemo -t -c \
  "SELECT customer_id, balance FROM accounts WHERE customer_id=1;"'
```

Mid-run (somewhere between $100 and $200), kill the worker pod:

```bash
kubectl -n bank-heist delete pod -l app.kubernetes.io/name=agent
```

The new pod resumes the workflow from its last checkpoint. When complete:

```bash
curl -s localhost:8000/status/customer-1
# → runtime_status: COMPLETED, final_balance: 200.0

kubectl -n bank-heist exec postgres-0 -- psql -U bankadmin -d bankdemo -c \
  "SELECT count(*) FROM transactions WHERE customer_id=1;"
# → 100  (exactly 100, no duplicates, even though the pod died mid-run)
```

That `100` is the demo's invariant working: deterministic `tx_id` + `ON CONFLICT DO NOTHING` makes the credit exactly-once at the DB regardless of how many times Dapr replayed the step.

### Rebuilding after code changes

```bash
docker build -t bank-heist/agent:0.1.0 services/agent
kind load docker-image bank-heist/agent:0.1.0 --name bank-heist
kubectl -n bank-heist rollout restart deployment/agent
```

## Run the UI (current state — simulator)

The React UI uses `<script type="text/babel" src="...">` tags, which browsers won't load over `file://`. Serve over HTTP:

```bash
python3 -m http.server 8000
# open http://localhost:8000/Bank%20Heist%20Demo.html
```

Today the UI runs entirely in the browser via `ui-prototype/src/telemetry.jsx:createTelemetry`. Step 5 of the plan replaces that with a WebSocket client backed by the orchestrator.

## Databases

| DB | Purpose |
|---|---|
| `bankdemo` | App data: `customers`, `accounts`, `transactions`, `audit_log` |
| `dapr_state` | Dapr workflow checkpoints — never targeted by chaos |

A trigger on `transactions` emits `pg_notify('tx_committed', ...)` on every insert, so the orchestrator can stream events to the UI without polling.

## Tear down

```bash
helm uninstall demo --namespace bank-heist
kubectl delete namespace bank-heist
kind delete cluster --name bank-heist
```

## Repo layout

```
.
├── ui-prototype/              # Standalone in-browser UI (pre-WS-wiring; see Step 5)
│   ├── bankheistdemo.html
│   ├── styles.css
│   ├── tweaks-panel.jsx
│   ├── src/                   # React components (shell, grids, panels, telemetry, app)
│   └── assets/                # Diagrid logos
├── deploy/
│   ├── kind/cluster.yaml      # 5-node kind cluster, 4 zones
│   ├── postgres/              # Helm chart: Postgres + schema + seed
│   ├── mcp/                   # Helm chart: MCP server
│   └── agent/                 # Helm chart: Dapr workflow worker + Component
├── services/
│   ├── mcp/                   # Python MCP server (asyncpg + FastAPI + mcp SDK)
│   └── agent/                 # Python Dapr workflow worker (dapr-ext-workflow + mcp client)
├── DECISIONS.md               # ADR-lite log of substantive decisions
├── PLAN.md                    # Full implementation plan
└── README.md
```

## Build status

- [x] Step 1: Postgres + schema + seed (Helm chart)
- [x] Step 1.5: Multi-node kind cluster
- [x] Step 2: MCP server (scaffold + Helm chart; image build pending)
- [x] Step 3: Single `DurableAgent` (stub LLM by default; real-model via `agent.llm.mode=real`)
- [ ] Step 4: Orchestrator + WS feed
- [ ] Step 5: Swap UI's `createTelemetry` for WS client
- [ ] Step 6: Spawn 100 workflow instances
- [ ] Step 7: Chaos endpoints
- [ ] Step 8: `fireAll` end-to-end validation
- [ ] Step 9: Real-LLM mode
- [ ] Step 10: Polish (chart, demo script)
