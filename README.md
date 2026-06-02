# Bank Heist Demo

Dapr Agents durability demo. 100 agents credit 10 customer accounts from $100 → $200, $1 at a time, while chaos is injected.

**Invariant** — every account finishes at exactly $200, total transaction count = 1000, regardless of pod kills, AZ evictions, dropped MCP calls, or latency injection. Exactly-once credits are enforced at the DB via a composite `(execution_run_id, tx_id)` primary key with `ON CONFLICT DO NOTHING`.

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│ UI (served by MCP) ─── WS /ws/telemetry ─── per-tx push          │
│   ▲                                                              │
│   │ HTTP                                                         │
│ ┌─┴────────────────┐    ┌─────────────────┐    ┌──────────────┐  │
│ │  MCP server      │◄───┤  Agent worker   │◄───┤  Dapr        │  │
│ │  · orchestrator  │    │  · DurableAgent │    │  · workflow  │  │
│ │  · chaos surface │    │  · stub LLM     │    │  · placement │  │
│ │  · ws broadcast  │    │                 │    │  · scheduler │  │
│ │  · pod-kill ctrl │    └────────┬────────┘    └──────────────┘  │
│ └─────────┬────────┘             │                               │
│           │                      │ schedule-one                  │
│           ▼                      │                               │
│      ┌────────────┐     ┌────────▼───────┐                       │
│      │ Postgres   │◄────┤   Replenisher  │                       │
│      │ bankdemo + │     │   (MCP-side)   │                       │
│      │ dapr_state │     └────────────────┘                       │
│      └────────────┘                                              │
└──────────────────────────────────────────────────────────────────┘
```

Pick a deployment path:

- [**Deploying to Kubernetes**](#deploying-to-kubernetes) — production-shaped, vendor-neutral. Cluster-flavor specifics live in their own docs:
  - [AKS.md](./AKS.md) — Azure cluster provisioning
  - [CATALYST_CLOUD.md](./CATALYST_CLOUD.md) — Diagrid Catalyst Cloud instead of upstream Dapr (no sidecar; SDK talks to managed endpoints)
- [**Local deployment with Dapr**](#local-deployment-with-dapr) — docker compose for Postgres + MCP, agent runs locally with a self-hosted `dapr run` sidecar.
- [**Local deployment with Catalyst**](#local-deployment-with-catalyst) — docker compose for Postgres + MCP, agent runs locally with Diagrid Catalyst providing managed Dapr APIs.

---

## Deploying to Kubernetes

These instructions are vendor-neutral. The charts work on any conformant cluster — AKS, EKS, GKE, k3s, kind, etc. For cluster-provisioning specifics see:

- **AKS**: [AKS.md](./AKS.md)
- Other clusters: provision yourself, then come back here at step 2.

### 0. Cluster prerequisites

The demo expects three "logical" node groups, identified by labels. How you produce these nodes is cluster-specific (separate nodepools on AKS/EKS/GKE, or just `kubectl label` on an existing pool):

| Label | Workload it hosts |
|---|---|
| `bank-heist.role=platform` | Postgres, MCP server (never chaos target) |
| `bank-heist.role=agents` | Agent worker pods (chaos target) |

If your cluster has Azure-AZ-style zones, AKS/EKS auto-set `topology.kubernetes.io/zone` on each node — the agent Deployment uses that for `topologySpreadConstraints` so one pod lands in each AZ. Not required; the demo still runs on a single zone.

Storage class: the Postgres chart defaults to `managed-csi` (AKS). Override with `--set persistence.storageClassName=<class>` if you're on another platform (e.g. `gp3` on EKS, `standard-rwo` on GKE, leave blank to use the cluster default).

### 1. Choose an image registry

The cluster needs to be able to pull from wherever you push to. Docker Hub is the default in the charts:

```bash
export REGISTRY=tezizzm                # docker hub default in the charts
# Or any other accessible registry:
# export REGISTRY=myacr.azurecr.io     # ACR
# export REGISTRY=ghcr.io/myorg        # GHCR
# export REGISTRY=123.dkr.ecr.us-east-1.amazonaws.com   # ECR
```

### 2. Build and push images

If your laptop arch differs from the cluster nodes (e.g. Apple Silicon → x86_64), build for the target with buildx:

```bash
docker buildx create --use --name multiarch 2>/dev/null || docker buildx use multiarch

docker buildx build --platform linux/amd64 \
  -t $REGISTRY/bank-heist-mcp:0.1.0 \
  -f services/mcp/Dockerfile --push .

docker buildx build --platform linux/amd64 \
  -t $REGISTRY/bank-heist-agent:0.1.0 \
  --push services/agent
```

(Drop `--platform linux/amd64` if your laptop already matches the cluster, or change it to `linux/arm64` for ARM clusters.)

### 3. Install Dapr

```bash
brew install dapr/tap/dapr-cli   # or: curl https://raw.githubusercontent.com/dapr/cli/master/install/install.sh | bash
dapr init -k --wait
dapr status -k                   # all components Healthy
```

### 4. Choose how the UI gets exposed

The MCP server is the only externally-reachable component (it hosts the UI plus all APIs). Three options, in order of simplicity:

**a. Direct LoadBalancer (simplest, recommended for the demo).** Cloud-managed L4 LB with a public IP, one Service:

```bash
# Pass these at install time in step 5, or `helm upgrade --reuse-values` later:
#   --set service.type=LoadBalancer
#   --set service.azureDnsLabel=<unique-label>   # AKS only; gives you a DNS name
```

**b. `kubectl port-forward` for local-only access.** No LB, no DNS:

```bash
kubectl -n bank-heist port-forward svc/mcp 8080:80
# open http://localhost:8080/
```

**c. nginx Ingress.** Useful when you have multiple services to route between or want centralized TLS. Install ingress-nginx and then deploy the `deploy/ingress` chart — see [TROUBLESHOOTING.md](./TROUBLESHOOTING.md) if the LB IP turns out unreachable externally on AKS.

### 5. Deploy the demo

```bash
NS=bank-heist
kubectl create namespace $NS

helm install postgres deploy/postgres -n $NS
kubectl -n $NS rollout status statefulset/postgres

helm install mcp deploy/mcp -n $NS \
  --set image.repository=$REGISTRY/bank-heist-mcp \
  --set service.type=LoadBalancer
  # AKS extra (optional): --set service.azureDnsLabel=<unique-label>

helm install agent deploy/agent -n $NS \
  --set image.repository=$REGISTRY/bank-heist-agent

kubectl -n $NS rollout status deployment/mcp
kubectl -n $NS rollout status deployment/agent
```

Verify pod placement:

```bash
kubectl -n $NS get pods -o wide
# postgres + mcp should be on a `bank-heist.role=platform` node
# agent replicas should be on `bank-heist.role=agents` nodes (one per zone if multi-AZ)
```

### 6. Open the UI

```bash
MCP_IP=$(kubectl -n $NS get svc mcp -o jsonpath='{.status.loadBalancer.ingress[0].ip}')
echo "http://$MCP_IP/"
```

If you set `service.azureDnsLabel`, the URL is `http://<label>.<region>.cloudapp.azure.com/` (no port — Service is on 80 by default).

> If the LB IP isn't reachable from your laptop, see the AKS [backend-pool gotcha](./TROUBLESHOOTING.md#public-lb-ip-exists-but-external-traffic-times-out). `kubectl -n $NS port-forward svc/mcp 8080:80` is the reliable fallback.

### 7. Verify the invariant

```bash
kubectl -n $NS exec postgres-0 -- psql -U bankadmin -d bankdemo -c \
  "SELECT execution_run_id, COUNT(*), SUM(amount) FROM transactions GROUP BY 1 ORDER BY 1 DESC LIMIT 3;"
# Latest execution_run_id row should have COUNT=1000, SUM=1000.00.

kubectl -n $NS exec postgres-0 -- psql -U bankadmin -d bankdemo -c \
  "SELECT SUM(balance) AS total, COUNT(*) FILTER (WHERE balance >= target) AS at_target FROM accounts;"
# total=2000.00, at_target=10
```

### Updating after code changes

```bash
docker buildx build --platform linux/amd64 \
  -t $REGISTRY/bank-heist-mcp:0.1.0 \
  -f services/mcp/Dockerfile --push .
kubectl -n $NS rollout restart deployment/mcp
```

(Agent: same pattern with `services/agent` build context, restart `deployment/agent`.)

### Tear down

```bash
helm uninstall agent mcp postgres -n $NS
helm uninstall ingress -n $NS 2>/dev/null   # only if you installed the ingress chart
kubectl delete namespace $NS
```

(Cluster-level tear-down is provider-specific — see your provider's doc, or [AKS.md](./AKS.md) if you used AKS.)

---

## Local deployment with Dapr

Self-hosted Dapr (`dapr init`) on your laptop. Postgres + MCP run in docker; the agent runs as a normal Python process with `dapr run` attaching a local sidecar.

### Prerequisites

- Docker (or Podman) with `docker compose`
- [`uv`](https://docs.astral.sh/uv/) (Python toolchain)
- Dapr CLI: `brew install dapr/tap/dapr-cli`

### 1. Initialize self-hosted Dapr

One-time per machine. Installs `daprd`, placement, and scheduler containers under `~/.dapr/`:

```bash
dapr init
dapr status   # all components Running
```

### 2. Bring up Postgres + MCP

```bash
docker compose -f local/compose.yaml up -d --build
curl -s http://localhost:9000/healthz   # → {"status":"ok"}
```

Postgres exposes port `5432`, MCP exposes `9000`. The compose init script seeds the `bankdemo` schema and creates an empty `dapr_state` database for the workflow checkpoint store.

### 3. Run the agent with a local Dapr sidecar

The agent's component manifest at `local/components/workflowstatestore.yaml` points the Dapr workflow runtime at the local `dapr_state` database.

```bash
cd services/agent
uv sync

dapr run --app-id agent-worker \
  --app-port 8000 \
  --dapr-http-port 3500 \
  --resources-path ../../local/components \
  -- env MCP_URL=http://localhost:9000/mcp/ \
        MCP_HTTP_BASE=http://localhost:9000 \
        STUB_LLM=true \
        uv run uvicorn agent_worker.main:app --host 0.0.0.0 --port 8000
```

(The MCP service in compose already has `AGENT_HTTP_BASE=http://host.docker.internal:8000` set, so MCP's replenisher reaches your local agent automatically.)

### 4. Open the UI

`http://localhost:9000/` — same UI as AKS, just port-forwarded through compose.

### 5. Verify the invariant

```bash
docker exec local-postgres-1 psql -U bankadmin -d bankdemo -c \
  "SELECT execution_run_id, COUNT(*), SUM(amount) FROM transactions GROUP BY 1 ORDER BY 1 DESC LIMIT 3;"
```

### Tear down

```bash
# Stop the agent: Ctrl-C in the dapr run terminal
docker compose -f local/compose.yaml down -v
dapr uninstall   # optional, only if you want to nuke local Dapr too
```

---

## Local deployment with Catalyst

Same MCP + Postgres in docker, but the agent's Dapr APIs go through Diagrid Catalyst's managed control plane instead of a self-hosted sidecar. Good for testing the same agent code in a hosted-Dapr environment from your laptop.

Full instructions live in [**CATALYST.md**](./CATALYST.md). Quick outline:

1. Provision the project with `--enable-managed-workflow --enable-agent-infrastructure`.
2. Create the `agent-worker` app-id. `--enable-agent-infrastructure` auto-provisions an `agent-memory` state store you can reuse.
3. `docker compose -f local/compose.yaml up -d --build` (same as the Dapr-local path).
4. `cd services/agent && uv sync`.
5. `diagrid dev run --file dapr.yaml --project <your-project> --skip-managed-kv --skip-managed-pubsub --skip-default-resiliency`.

Set `FORCE_WORKFLOW_NAME=agent_workflow` in the agent's env before step 5 — this runtime requires the short workflow alias rather than the fully-qualified name used elsewhere. See [CATALYST.md](./CATALYST.md) for the full walkthrough and [NOTES_FOR_DAPR_AGENTS.md](./NOTES_FOR_DAPR_AGENTS.md) for the engineering follow-up on this discrepancy.

---

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
│   ├── init.sql               # bankdemo schema + dapr_state DB
│   └── components/            # Dapr component manifests for `dapr run` (self-hosted local)
├── services/
│   ├── mcp/                   # FastAPI + mcp SDK + asyncpg + orchestrator + replenisher + WS
│   └── agent/                 # dapr-agents DurableAgent + stub LLM
├── AKS.md                     # Azure-specific cluster provisioning
├── CATALYST.md                # Catalyst local bring-up (`diagrid dev run`)
├── CATALYST_CLOUD.md          # Catalyst Cloud on k8s
├── TROUBLESHOOTING.md         # Common failures across all deploy paths
└── NOTES_FOR_DAPR_AGENTS.md   # Workflow-name compat note for the dapr-agents team
```
