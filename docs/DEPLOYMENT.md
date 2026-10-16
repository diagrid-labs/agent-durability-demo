# Deployment

Two ways to run the Bank Creditor demo: deploy to Kubernetes (production-shaped, any conformant cluster), or run the agent locally against a Catalyst project. The agent reaches its Postgres-backed tools (`get_balance`, `credit_account`, etc.) through Catalyst's managed MCP proxy — there's no non-Catalyst transport for this anymore, so both paths below require a Catalyst project (Self-Hosted or Cloud); pick based on where the *cluster* lives.

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

The cluster needs to be able to pull from wherever you push to. The charts default to the demo's public ECR repositories in the Diagrid demo AWS account, tagged with the commit each image was built from:

```bash
export REGISTRY=public.ecr.aws/d3f9w4q8/bank-creditor-demo   # chart default; pushing needs that AWS account
export TAG=$(git rev-parse --short HEAD)
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
  -t $REGISTRY/mcp:$TAG \
  -f services/mcp/Dockerfile --push .

docker buildx build --platform linux/amd64 \
  -t $REGISTRY/agent:$TAG \
  --push services/agent-langgraph
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
  --set image.repository=$REGISTRY/mcp --set image.tag=$TAG \
  --set service.azureDnsLabel=<unique-label>   # required on AKS to avoid colliding with the demo's default label

helm install agent deploy/agent -n $NS \
  --set image.repository=$REGISTRY/agent --set image.tag=$TAG

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

### 7. Optional: LangSmith tracing

The LangGraph agent can send its traces to LangSmith, so you can show the same run in the LangSmith UI next to Catalyst. Each workflow is one `LangGraph` trace, with a child run per graph node (`agent`, `tools`) and a `credit_next` tool run tagged with the pod that ran it.

```bash
kubectl -n $NS create secret generic langsmith-api-key \
  --from-literal=LANGSMITH_API_KEY=<your LangSmith API key>

helm upgrade agent deploy/agent -n $NS --reset-then-reuse-values \
  --set langsmith.enabled=true \
  --set langsmith.project=bank-creditor
# EU LangSmith orgs also need: --set langsmith.endpoint=https://eu.api.smith.langchain.com
# A service key (lsv2_sk_) also needs: --set langsmith.workspaceId=<workspace ID>
```

A service key is scoped to the whole LangSmith organization, so without the workspace ID every trace upload gets a `403 Forbidden`.

Use `--reset-then-reuse-values` (Helm 3.14+), not `--reuse-values`. A release installed before this chart had a `langsmith` block has none of its defaults stored, so `--reuse-values` leaves the secret name empty and the upgrade fails with `secretKeyRef.name: Invalid value: ""`.

After the first run the project exists in LangSmith. Copy its URL and give it to the MCP chart, which adds an **Open in LangSmith** button to the UI top bar:

```bash
helm upgrade mcp deploy/mcp -n $NS --reset-then-reuse-values \
  --set langsmithUrl=<LangSmith project URL>
```

The plain LangGraph comparison (`deploy/agent-plain`, no Catalyst) has the same `langsmith` block and traces to its own project, `bank-creditor-plain`. Create the same secret in its namespace, set `langsmith.enabled=true` on that release, and set `langsmithUrl` on that namespace's MCP release.

To turn tracing off again: `helm upgrade agent deploy/agent -n $NS --reset-then-reuse-values --set langsmith.enabled=false`.

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
export TAG=$(git rev-parse --short HEAD)
docker buildx build --platform linux/amd64 \
  -t $REGISTRY/mcp:$TAG \
  -f services/mcp/Dockerfile --push .
helm upgrade mcp deploy/mcp -n $NS --reset-then-reuse-values --set image.tag=$TAG
```

(Agent: same pattern with the `services/agent-langgraph` build context, pushed as `$REGISTRY/agent`, then `helm upgrade agent deploy/agent`. The plain variant is `services/agent-plain` → `$REGISTRY/agent-plain`.)

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
4. `cd services/agent-langgraph && uv sync`.
5. Register the MCP service as a Catalyst `MCPServer` and grant `agent-worker` access to its tools — see [CATALYST.md](./CATALYST.md)'s MCP section for the exact `diagrid mcpserver create` / `access grant` commands.
6. `diagrid dev run --file dapr.yaml --project <your-project> --skip-managed-kv --skip-managed-pubsub --skip-default-resiliency`.

See [CATALYST.md](./CATALYST.md) for the full walkthrough.
