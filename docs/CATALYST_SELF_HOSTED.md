# Catalyst Self-Hosted bring-up

Same agent + MCP code as the upstream-Dapr path, but the Dapr control plane (workflow engine, state store, pubsub) runs **inside your AKS cluster** under a Diagrid Catalyst Self-Hosted region. Pods don't get a `daprd` sidecar — the Dapr SDK reads `DAPR_HTTP_ENDPOINT` / `DAPR_GRPC_ENDPOINT` / `DAPR_API_TOKEN` from env and talks to the in-cluster Catalyst gateway directly.

Use this path when you want:
- Hosted-Dapr semantics + Catalyst Console + workflow visibility
- No per-app-id RPS limits (unlike Cloud Catalyst)
- Everything on a single AKS cluster

## Prerequisites

- A K8s cluster on a recent version (AKS, EKS, GKE, k3s — anything conformant)
- `kubectl` context pointing at the cluster
- `diagrid` CLI authenticated to the target org (`diagrid login` + `diagrid org use <name>`)
- The cluster's default StorageClass supports online volume expansion. On AKS, `managed-csi` does. On EKS, the default `gp3` does. On GKE, `standard-rwo` does. On kind / k3s, expansion is a no-op (PVCs are local-path).

Confirm the context before deploying anything:

```bash
kubectl config current-context
diagrid whoami
```

## 1. Create the region

```bash
diagrid region create my-sh-region \
  --enable-managed-domain \
  --enable-public-management-api \
  --ingress my-sh-region.demo.local \
  --location <your-region>     # informational label, e.g. northeurope, us-east-1, europe-west1
```

If your plan doesn't support managed-domain at create time (silently dropped), enable it after:

```bash
diagrid region update my-sh-region --enable-managed-domain --enable-public-management-api
diagrid region get my-sh-region    # confirm INGRESS shows *.<wildcard>.r1.privatediagrid.net
```

## 2. Deploy the control plane to your cluster

The chart defaults the gateway LB ports to `8080/8443`, but the `diagrid` CLI hardcodes `:443` — so apply the port override saved in `deploy/catalyst-selfhosted/port-override.yaml`:

```bash
diagrid region deploy my-sh-region \
  --values-file ./deploy/catalyst-selfhosted/port-override.yaml
```

Provisioning takes ~3-5 min. Verify:

```bash
kubectl get pods -n cra-agent
kubectl get pods -n root-dapr-system
kubectl get pods -n shared-kafka
kubectl get pods -n shared-postgresql
```

## 3. Resize the shared-postgresql + Kafka PVCs immediately

The Catalyst chart defaults all storage PVCs to 1Gi. Bank Creditor workflows fill that in well under a day. **Do this before workloads start writing:**

```bash
kubectl -n shared-postgresql patch pvc data-shared-postgresql-0 \
  -p '{"spec":{"resources":{"requests":{"storage":"50Gi"}}}}'
kubectl -n shared-postgresql delete pod shared-postgresql-0

for i in 0 1 2; do
  kubectl -n shared-kafka patch pvc data-shared-kafka-controller-$i \
    -p '{"spec":{"resources":{"requests":{"storage":"10Gi"}}}}'
done
# Restart Kafka pods one at a time — 3-node quorum
kubectl -n shared-kafka delete pod shared-kafka-controller-0   # wait for Running
kubectl -n shared-kafka delete pod shared-kafka-controller-1   # wait for Running
kubectl -n shared-kafka delete pod shared-kafka-controller-2
```

## 4. Create the project + app ID

`--enable-agent-infrastructure` is **required at create time** — it auto-provisions the `agent-memory` state store the workflow runtime expects:

```bash
diagrid project create resiliency-demo \
  --region my-sh-region \
  --enable-managed-workflow \
  --enable-agent-infrastructure \
  --use --wait

diagrid appid create bank-agent-creditor --wait
diagrid appid list   # should show ready
```

Save the project ID (e.g. `prj1548627`) for the hostAliases step. There's no separate app-id for the MCP server anymore — registering it as a Catalyst `MCPServer` resource (step 8) provisions its own implicit app-id automatically.

## 5. Create the K8s secret

```bash
# Agent's outbound auth token (fetch from Catalyst)
diagrid appid get bank-agent-creditor -o json | jq -r '.status.apiToken' > /tmp/agent-api-token.txt
chmod 600 /tmp/agent-api-token.txt

kubectl create namespace bank-creditor --dry-run=client -o yaml | kubectl apply -f -

kubectl -n bank-creditor delete secret catalyst-agent-worker --ignore-not-found

kubectl -n bank-creditor create secret generic catalyst-agent-worker \
  --from-literal=DAPR_API_TOKEN="$(tr -d '\n' < /tmp/agent-api-token.txt)"
```

## 6. Write the agent Helm overlay

The agent needs Catalyst endpoint URLs + a `hostAliases` mapping that points the Catalyst hostnames at the **in-cluster gateway ClusterIP** — required to work around AKS hairpin NAT (pods can't reach their own cluster's public LB IP).

Find the gateway ClusterIP + wildcard:

```bash
GATEWAY_IP=$(kubectl -n cra-agent get svc gateway-envoy -o jsonpath='{.spec.clusterIP}')
WILDCARD=$(diagrid region get my-sh-region -o json | jq -r '.spec.ingress.wildcardDomain')
PRJ=$(diagrid project get resiliency-demo -o json | jq -r '.metadata.id')
echo "$GATEWAY_IP / $WILDCARD / $PRJ"
```

Write the overlay:

```bash
cat > /tmp/agent-catalyst-overlay.yaml <<EOF
catalyst:
  enabled: true
  httpEndpoint: https://http-${PRJ}.${WILDCARD}:443
  grpcEndpoint: https://grpc-${PRJ}.${WILDCARD}:443
  apiTokenSecret: catalyst-agent-worker
  apiTokenSecretKey: DAPR_API_TOKEN
  hostAliases:
    - ip: ${GATEWAY_IP}
      hostnames:
        - http-${PRJ}.${WILDCARD}
        - grpc-${PRJ}.${WILDCARD}
EOF
```

> **Why `hostAliases` is required on AKS:** AKS hairpin NAT blocks pods from reaching their own cluster's public LoadBalancer IP — connections to the gateway's external IP time out. The `hostAliases` block bypasses this by short-circuiting the Catalyst hostnames to the gateway's ClusterIP at the pod's `/etc/hosts` level.
>
> **On EKS / GKE / clusters without hairpin issues**, this is harmless redundancy but still recommended — it removes a dependency on cluster DNS reaching the public hostname.

## 7. Install the demo charts

```bash
helm -n bank-creditor upgrade --install postgres ./deploy/postgres --wait

# AKS: keep service.azureDnsLabel — gets you a public DNS hostname for free.
# Non-AKS: override service.type to ClusterIP (use ingress) or NodePort,
#         and drop the azureDnsLabel — it's an AKS-only annotation.
helm -n bank-creditor upgrade --install mcp ./deploy/mcp \
  -f ./deploy/mcp/values.yaml \
  --set service.azureDnsLabel=<unique-label-in-region> \
  --wait

helm -n bank-creditor upgrade --install agent ./deploy/agent \
  -f ./deploy/agent/values.yaml \
  -f /tmp/agent-catalyst-overlay.yaml \
  --wait
```

The chart defaults to `stateStore.componentName: agent-memory` and `stateStore.create: false` — matches what Catalyst auto-provisions. No `--set stateStore.*` overrides needed. (Note: the agent code doesn't read this store directly — durability comes entirely from Dapr Workflow activity persistence. `stateStore.create` still controls whether `templates/component-state.yaml` renders the in-cluster CRD, so it stays load-bearing for a non-Catalyst chart deploy.)

### Azure-specific bits in the charts

The MCP chart defaults to `service.type: LoadBalancer` with `service.azureDnsLabel: demo-prod-catalyst-agents`. This annotation tells AKS's cloud controller to create an Azure DNS record at `<label>.<region>.cloudapp.azure.com`. On non-Azure clusters:

- **EKS / GKE**: the annotation is silently ignored; you'll still get a `LoadBalancer` IP, just no DNS — use your own DNS or an ingress.
- **kind / k3s / minikube**: no cloud LB available — override `--set service.type=ClusterIP` and use `kubectl port-forward` or the `deploy/ingress` chart.

## 8. Register the MCP server and grant access

The agent reaches its Postgres-backed tools (`get_balance`, `credit_account`, `get_next_task`, `report_done`) through Catalyst's managed MCP proxy, not direct Dapr service invocation. Register the `mcp` Service's FastMCP endpoint as an `MCPServer` resource, then grant `bank-agent-creditor` access — new MCP servers deny every tool until granted:

```bash
diagrid mcpserver create bank-postgres-mcp \
  --project resiliency-demo \
  --url http://mcp.bank-creditor.svc.cluster.local/mcp/ \
  --wait

diagrid mcpserver access grant bank-postgres-mcp \
  --project resiliency-demo \
  --caller bank-agent-creditor \
  --allow-tools get_balance,credit_account,get_next_task,report_done \
  --wait
```

This must come **after** the `mcp` chart is installed (step 7) — Catalyst validates the upstream URL when registering. `bank-postgres-mcp` becomes its own implicit app-id automatically; you don't create one for it separately.

If the agent's tool calls come back `403 Forbidden`, the grant above didn't take — re-run `diagrid mcpserver access get bank-postgres-mcp --project resiliency-demo` to confirm `bank-agent-creditor` is listed with all four tools.

## 9. Verify

```bash
kubectl -n bank-creditor get pods
kubectl -n bank-creditor logs deploy/agent --since=2m | grep -iE 'workflow|catalyst|registered|endpoint' | head -20
```

You want:
- `Registered node: agent` / `Registered node: tools` (confirms the LangGraph graph registered correctly — see [TROUBLESHOOTING.md](./TROUBLESHOOTING.md#node-name-not-found-in-registry) if you see `Could not extract callable for node` instead)
- `Registering workflow 'dapr.langgraph.Banker.workflow'`
- `Starting gRPC worker that connects to dns:grpc-<prj>.<wildcard>:443`
- **No** `Connection refused` / `UNAVAILABLE` / `timeout`

Then hit the UI at the MCP LB's external hostname (`http://<label>.<region>.cloudapp.azure.com/`), click **Start run**, and watch transactions climb.

## Tear-down

To wipe the Self-Hosted control plane (preserves the `bank-creditor` namespace + app data):

```bash
helm -n cra-agent uninstall catalyst-otel-logs-collector catalyst-otel-metrics-collector catalyst
helm -n shared-kafka uninstall shared-kafka
helm -n shared-postgresql uninstall shared-postgresql

kubectl delete ns cra-agent root-dapr-system shared-kafka shared-postgresql catalyst-system

# CRDs linger after ns delete — required clean if you'll re-deploy:
kubectl delete crd \
  components.dapr.io configurations.dapr.io httpendpoints.dapr.io \
  mcpservers.dapr.io resiliencies.dapr.io subscriptions.dapr.io

diagrid region delete my-sh-region
```

## Common failure modes

- **Workflow scheduled but errors with "state store not found"** — `--enable-agent-infrastructure` wasn't passed at project-create time. See [TROUBLESHOOTING.md](./TROUBLESHOOTING.md#failed-to-create-orchestration-instance-the-state-store-is-not-found--state-store--is-not-found).
- **Agent logs show `Connection timed out` retrying the LB external IP** — `hostAliases` didn't take. Confirm with `kubectl -n bank-creditor get deploy agent -o yaml | grep -A4 hostAliases`.
- **Tool calls fail with `upstream HTTP 405`** — Catalyst's MCP proxy relays the caller's request to the registered upstream URL with the trailing slash stripped, even though the URL was registered with one. FastMCP's own Mount only gives Starlette a partial match for the bare path, so it falls through to the `/` static-files catch-all, which rejects POST outright. Fixed server-side by `_MCPTrailingSlashMiddleware` in `services/mcp/mcp_server/server.py` — if you see this on a *different* MCP server, add an equivalent path-normalizing shim in front of it.
- **Tool calls fail with `403 Forbidden`** — no access grant yet, or it doesn't cover the tool being called. Run `diagrid mcpserver access grant` from step 8.
- **`diagrid` CLI times out to LB IP from your laptop** — port-override.yaml in step 2 not applied. The chart defaults to 8080/8443, the CLI hardcodes 443.
- **gRPC `UNIMPLEMENTED`** — agent endpoint is pointing at the per-app inbound hostname instead of `grpc-prj<id>.<wildcard>` (the project-scoped outbound).

See [TROUBLESHOOTING.md](./TROUBLESHOOTING.md) for the full list.
