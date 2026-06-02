# Deploying with Catalyst Cloud

Catalyst Cloud-specific setup for the [Bank Heist Demo](./README.md) on Kubernetes. Replaces upstream Dapr — instead of `dapr init -k` and per-pod sidecars, the agent pod gets three env vars pointing at the Catalyst project's hosted endpoints, and the Dapr SDK talks to them directly.

Once the Catalyst project exists and you've got the endpoint URLs + API token, the rest of the deploy mirrors [Deploying to Kubernetes](./README.md#deploying-to-kubernetes) with a few values toggled on the agent install.

## What's different from upstream Dapr

| | Upstream Dapr | Catalyst Cloud |
|---|---|---|
| Control plane | `dapr-system` namespace in your cluster (`dapr init -k`) | Hosted by Diagrid |
| Data plane (where `daprd` runs) | A sidecar **in every agent pod** | None in your pod — SDK reads `DAPR_HTTP_ENDPOINT` and talks to Catalyst directly |
| Components | k8s `Component` CRDs in the namespace | Managed via `diagrid` CLI in the Catalyst project |
| `dapr.enabled` in chart | `true` | `false` |
| `stateStore.create` in chart | `true` | `false` |
| `catalyst.enabled` in chart | `false` | `true` |
| Throughput | Sub-millisecond per activity (intra-pod) | WAN-bound (~30-100ms per activity round trip) |

The durability semantics are identical between modes; only the throughput differs. See [TROUBLESHOOTING.md](./TROUBLESHOOTING.md#catalyst-cloud-throughput-much-lower-than-upstream-dapr) for context.

## Prerequisites

- A Kubernetes cluster (see [AKS.md](./AKS.md) for AKS, or use any conformant cluster).
- `diagrid` CLI installed and authenticated: `diagrid login`.
- An org with Catalyst Cloud access.

## 1. Provision the Catalyst project

```bash
PROJECT=demo-production-catalyst-agents     # match your cluster name for clarity
REGION=diagrid-aws-eu-west                  # pick a region close to your cluster

# (Optional) switch orgs if you have multiple
diagrid org list
diagrid org use <new-org>
diagrid whoami

# Create the project. Both flags are creation-only — they cannot be added
# to an existing project later. Re-provisioning is the only way to enable
# `--enable-agent-infrastructure` retroactively.
diagrid project create $PROJECT \
  -r $REGION \
  --enable-managed-workflow \
  --enable-agent-infrastructure \
  --use --wait

# Create the app ID the agent worker will connect as
diagrid appid create agent-worker --wait

# Verify
diagrid project get $PROJECT
diagrid component list
```

The `--enable-agent-infrastructure` flag auto-provisions five managed components:

```
NAME              TYPE              SCOPES                STATUS
agent-memory      state.diagrid     all app identities    ready
agent-pubsub      pubsub.diagrid    all app identities    ready
agent-registry    state.diagrid     all app identities    ready
agent-runtime     state.diagrid     all app identities    ready
agent-workflow    state.diagrid     all app identities    ready
```

Reuse `agent-memory` as the workflow state store — fewer moving parts than creating a separately-named component. The agent chart's `stateStore.componentName` controls which one the agent points at; set it to `agent-memory` at install time (step 5 below).

## 2. Retrieve the connection details

```bash
HTTP_ENDPOINT=$(diagrid project get $PROJECT -o json \
  | jq -r '.status.endpoints.http.url')
GRPC_ENDPOINT=$(diagrid project get $PROJECT -o json \
  | jq -r '.status.endpoints.grpc.url')
API_TOKEN=$(diagrid appid get agent-worker --project $PROJECT -o json \
  | jq -r '.status.apiToken')

echo "HTTP: $HTTP_ENDPOINT"
echo "GRPC: $GRPC_ENDPOINT"
echo "TOKEN length: ${#API_TOKEN}"
```

(JSON paths can vary by CLI version; pipe through `jq` and explore if these don't resolve.)

## 3. Stash the API token as a k8s Secret

```bash
NS=bank-heist
kubectl create namespace $NS

kubectl -n $NS create secret generic catalyst-agent-worker \
  --from-literal=DAPR_API_TOKEN="$API_TOKEN"
```

The agent chart references this Secret by name (`catalyst.apiTokenSecret`, default `catalyst-agent-worker`).

## 4. Build and push images

Same as the upstream-Dapr path — see [README · step 2](./README.md#2-build-and-push-images).

## 5. Deploy the demo

Postgres + MCP install with the usual flags. The **agent** install switches the chart into Catalyst mode:

```bash
helm install postgres deploy/postgres -n $NS
kubectl -n $NS rollout status statefulset/postgres

helm install mcp deploy/mcp -n $NS \
  --set image.repository=$REGISTRY/bank-heist-mcp \
  --set service.type=LoadBalancer

helm install agent deploy/agent -n $NS \
  --set image.repository=$REGISTRY/bank-heist-agent \
  --set dapr.enabled=false \
  --set stateStore.create=false \
  --set stateStore.componentName=agent-memory \
  --set catalyst.enabled=true \
  --set catalyst.httpEndpoint="$HTTP_ENDPOINT" \
  --set catalyst.grpcEndpoint="$GRPC_ENDPOINT"

kubectl -n $NS rollout status deployment/mcp
kubectl -n $NS rollout status deployment/agent
```

The agent code's env-aware logic picks up Catalyst-mode behavior automatically because `DAPR_HTTP_ENDPOINT` is set:
- The startup sidecar wait (`/v1.0/healthz/outbound` poll) is skipped — there's no local daprd.
- The Dapr SDK reads the endpoint URLs + API token from the injected env vars on every workflow call.

## 6. Open the UI

```bash
MCP_IP=$(kubectl -n $NS get svc mcp -o jsonpath='{.status.loadBalancer.ingress[0].ip}')
echo "http://$MCP_IP/"
```

Optionally attach a DNS-friendly name (AKS):

```bash
helm upgrade mcp deploy/mcp -n $NS --reuse-values \
  --set service.azureDnsLabel=<unique-label-in-region>
# Wait ~30s, then:
echo "http://<unique-label-in-region>.<region>.cloudapp.azure.com/"
```

## 7. Verify the invariant

```bash
kubectl -n $NS exec postgres-0 -- psql -U bankadmin -d bankdemo -c \
  "SELECT execution_run_id, COUNT(*), SUM(amount) FROM transactions GROUP BY 1 ORDER BY 1 DESC LIMIT 3;"
# Latest execution_run_id row should reach COUNT=1000, SUM=1000.00.
```

## Workflow view in Catalyst

Every workflow is visible in the Catalyst console at <https://catalyst.diagrid.io> → project → **Workflows** tab for the `agent-worker` App ID. Each `agent-NNN-task-K-rXXX` instance shows its full activity trace — useful for the durability narrative (audience can watch retries land while the local UI animates the customer balance climbing).

## Throughput expectations

Catalyst Cloud is a hosted multi-tenant control plane. Every workflow activity is a WAN round-trip from your cluster to Diagrid's region, plus serialized writes to the managed state store. For the demo's chaos story this doesn't matter — the invariant holds at any throughput — but a full 1000-credit run typically takes 6-10 minutes on Cloud vs. 30-60 seconds on upstream Dapr.

If you want production-like throughput, the upgrade path is Catalyst Enterprise / Self-Hosted (data plane runs inside your cluster). Same agent chart flags, only the endpoint URLs change. See <https://docs.diagrid.io/operate/hosting/enterprise-self-hosted/>.

## Tear-down

```bash
helm uninstall agent mcp postgres -n $NS
kubectl -n $NS delete secret catalyst-agent-worker
kubectl delete namespace $NS

# Optional: delete the Catalyst project entirely
diagrid project delete $PROJECT
```

## Common gotchas

- **`OrchestratorNotRegisteredError`** — schedule name doesn't match the SDK's registered name. The chart uses `dapr.agents.Banker.workflow` by default, which is what works in Cloud. If something has overridden it (`FORCE_WORKFLOW_NAME` env var), unset it.
- **`state store ... not found`** — the chart's `stateStore.componentName` doesn't match a component in the Catalyst project. Run `diagrid component list` and align.
- **Per-tx UI updates feel slow** — that's Catalyst Cloud WAN latency, not a bug. See [TROUBLESHOOTING.md](./TROUBLESHOOTING.md#catalyst-cloud-throughput-much-lower-than-upstream-dapr).
- **`helm upgrade --reuse-values` keeps an old default** — Helm preserves stored values even when chart defaults change. Override explicitly. See [TROUBLESHOOTING.md](./TROUBLESHOOTING.md#helm-upgrade---reuse-values-keeps-an-old-value).
