# Bank Creditor Demo

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

The agent reaches its Postgres-backed tools (`get_balance`, `credit_account`, etc.) through Catalyst's managed MCP proxy — there's no non-Catalyst transport for this anymore. Every path below therefore requires a Catalyst project (Self-Hosted or Cloud); pick based on where the *cluster* lives.

Pick a deployment path:

- [**Deploying to Kubernetes**](#deploying-to-kubernetes) — production-shaped. Bring your own cluster (AKS, EKS, GKE, kind, k3s).
  - [CATALYST_SELF_HOSTED.md](./CATALYST_SELF_HOSTED.md) — Diagrid Catalyst Self-Hosted in the same cluster (no daprd sidecar; SDK talks to in-cluster gateway), including the MCP server registration + access-grant steps.
- [**Local deployment with Catalyst**](#local-deployment-with-catalyst) — docker compose for Postgres + MCP, agent runs locally with Diagrid Catalyst providing managed Dapr APIs and the MCP proxy.

---

## Deploying to Kubernetes

These instructions are vendor-neutral. The charts work on any conformant cluster — AKS, EKS, GKE, k3s, kind, etc. Provision the cluster with whichever tool you prefer, then come back here at step 2.

### 0. Cluster prerequisites

The demo expects three "logical" node groups, identified by labels. How you produce these nodes is cluster-specific (separate nodepools on AKS/EKS/GKE, or just `kubectl label` on an existing pool):

| Label | Workload it hosts |
|---|---|
| `bank-creditor.role=platform` | Postgres, MCP server (never chaos target) |
| `bank-creditor.role=agents` | Agent worker pods (chaos target) |

If your cluster has Azure-AZ-style zones, AKS/EKS auto-set `topology.kubernetes.io/zone` on each node — the agent Deployment uses that for `topologySpreadConstraints` so one pod lands in each AZ. Not required; the demo still runs on a single zone.

Storage class: the Postgres chart defaults to `managed-csi` (AKS). Override with `--set persistence.storageClassName=<class>` if you're on another platform (e.g. `gp3` on EKS, `standard-rwo` on GKE, leave blank to use the cluster default).

### 1. Choose an image registry

The cluster needs to be able to pull from wherever you push to. Docker Hub is the default in the charts:

```bash
export REGISTRY=alicejgibbons          # docker hub default in the charts
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
  -t $REGISTRY/bank-creditor-mcp:0.1.0 \
  -f services/mcp/Dockerfile --push .

docker buildx build --platform linux/amd64 \
  -t $REGISTRY/bank-creditor-agent:0.1.0 \
  --push services/agent
```

(Drop `--platform linux/amd64` if your laptop already matches the cluster, or change it to `linux/arm64` for ARM clusters.)

### 3. Install Catalyst and create app-ids

Follow [CATALYST_SELF_HOSTED.md](./CATALYST_SELF_HOSTED.md)'s project + app-id setup before continuing — this demo has no plain-Dapr fallback (`dapr init -k` alone isn't enough) since the agent's tool calls now go through Catalyst's MCP proxy. Come back here once `bank-agent-creditor` shows `ready` in `diagrid appid list`.

### 4. Choose how the UI gets exposed

The MCP server is the only externally-reachable component (it hosts the UI plus all APIs). Three options, in order of simplicity:

**a. Direct LoadBalancer (chart default, recommended for the demo).** The MCP chart defaults to `service.type=LoadBalancer` and `service.azureDnsLabel=demo-prod-catalyst-agents`. Override the label for your environment (must be unique within the Azure region):

```bash
# In step 5, or `helm upgrade --reuse-values` later:
#   --set service.azureDnsLabel=<unique-label>
```

**b. `kubectl port-forward` for local-only access.** No LB, no DNS:

```bash
kubectl -n bank-creditor port-forward svc/mcp 8080:80
# open http://localhost:8080/
```

**c. nginx Ingress.** Useful when you have multiple services to route between or want centralized TLS. Install ingress-nginx and then deploy the `deploy/ingress` chart — see [TROUBLESHOOTING.md](./TROUBLESHOOTING.md) if the LB IP turns out unreachable externally on AKS.

### 5. Deploy the demo

```bash
NS=bank-creditor
kubectl create namespace $NS

helm install postgres deploy/postgres -n $NS
kubectl -n $NS rollout status statefulset/postgres

helm install mcp deploy/mcp -n $NS \
  --set image.repository=$REGISTRY/bank-creditor-mcp \
  --set service.azureDnsLabel=<unique-label>   # required on AKS to avoid colliding with the demo's default label

helm install agent deploy/agent -n $NS \
  --set image.repository=$REGISTRY/bank-creditor-agent

kubectl -n $NS rollout status deployment/mcp
kubectl -n $NS rollout status deployment/agent
```

Verify pod placement:

```bash
kubectl -n $NS get pods -o wide
# postgres + mcp should be on a `bank-creditor.role=platform` node
# agent replicas should be on `bank-creditor.role=agents` nodes (one per zone if multi-AZ)
```

Now that `mcp` is deployed and reachable, register it with Catalyst and grant the agent access — see [CATALYST_SELF_HOSTED.md](./CATALYST_SELF_HOSTED.md)'s MCP server section for the exact `diagrid mcpserver create` / `access grant` commands. Workflows will fail until this grant exists.

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
  -t $REGISTRY/bank-creditor-mcp:0.1.0 \
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

(Cluster-level tear-down is provider-specific — see your provider's doc.)

---

## Local deployment with Catalyst

MCP + Postgres run in docker; the agent's Dapr APIs — including its MCP tool calls — go through Diagrid Catalyst's managed control plane instead of a local sidecar. This is the only supported local-dev path: the agent's tool-calling now depends on Catalyst's MCP proxy, which has no self-hosted-Dapr equivalent.

Full instructions live in [**CATALYST.md**](./CATALYST.md). Quick outline:

1. Provision the project with `--enable-managed-workflow --enable-agent-infrastructure`.
2. Create the `agent-worker` app-id. `--enable-agent-infrastructure` auto-provisions an `agent-memory` state store you can reuse.
3. `docker compose -f local/compose.yaml up -d --build`.
4. `cd services/agent && uv sync`.
5. Register the MCP service as a Catalyst `MCPServer` and grant `agent-worker` access to its tools — see [CATALYST.md](./CATALYST.md)'s MCP section for the exact `diagrid mcpserver create` / `access grant` commands.
6. `diagrid dev run --file dapr.yaml --project <your-project> --skip-managed-kv --skip-managed-pubsub --skip-default-resiliency`.

Set `FORCE_WORKFLOW_NAME=agent_workflow` in the agent's env before step 6 — this runtime requires the short workflow alias rather than the fully-qualified name used elsewhere. See [CATALYST.md](./CATALYST.md) for the full walkthrough.

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
│   └── init.sql               # bankdemo schema + dapr_state DB
├── services/
│   ├── mcp/                   # FastAPI + mcp SDK + asyncpg + orchestrator + replenisher + WS
│   └── agent/                 # dapr-agents DurableAgent + stub LLM
├── CATALYST.md                # Catalyst local bring-up (`diagrid dev run`)
├── CATALYST_SELF_HOSTED.md    # Catalyst Self-Hosted in your cluster
└── TROUBLESHOOTING.md         # Common failures across all deploy paths
```
