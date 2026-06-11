# Troubleshooting

Common failures hit while bringing the Bank Heist demo up across the deploy paths (AKS + Catalyst Self-Hosted, local + Dapr, local + Catalyst). Most are environmental rather than code bugs.

## UI loads but customer balances never move

Symptom: the heatmap animates briefly or shows `100 alive`, but `applied_total` stays at 0 and the customer accounts column sits at $100.

Diagnostic order:
1. `curl /agent/status` — is `replenisher_running` true? `spawn_count` non-zero?
2. `curl /orch/status` — `in_flight > 0`? `applied_total` climbing?
3. `kubectl logs deploy/agent -c agent --tail 60` — workflow errors?
4. `kubectl logs deploy/mcp --tail 30` — replenisher errors?

If `spawn_count == 0` with `replenisher_running: true`, the MCP-side replenisher is calling `/schedule-one` on the agent and getting failures. Look at the MCP log for the response body — usually a 502 with a daprd error message embedded.

Common causes below.

## `OrchestratorNotRegisteredError: A 'X' orchestrator was not registered`

The workflow engine dispatched a work item to the Python worker, but the name in the dispatch doesn't match anything in the worker's local registry.

dapr-agents 1.x registers the orchestrator under `dapr.agents.<name-lower>.workflow` (lowercase, e.g. `dapr.agents.banker.workflow`). Confirm the registered name from the agent's startup log:

```
WorkflowRuntime INFO: Registering workflow 'dapr.agents.banker.workflow' with runtime
```

Schedule with that exact name. The code in `services/agent/agent_worker/main.py` does this by default. If something downstream has overridden it (e.g. `FORCE_WORKFLOW_NAME` env var), reset:

```bash
kubectl -n bank-heist set env deployment/agent FORCE_WORKFLOW_NAME-
kubectl -n bank-heist rollout restart deployment/agent
```

If running locally under `diagrid dev run` and the fully-qualified name fails there, set `FORCE_WORKFLOW_NAME=agent_workflow` — local Catalyst's daprd has historically accepted only the short alias.

## `failed to create orchestration instance: the state store is not found` / `state store ... is not found`

The workflow runtime needs a state store named whatever `AGENT_STATE_STORE` says. The chart's defaults are tuned for Catalyst Self-Hosted (`stateStore.componentName: agent-memory`, `stateStore.create: false`).

**Catalyst (Self-Hosted or Local)** — the named component doesn't exist in the project. `agent-memory` is auto-provisioned by `--enable-agent-infrastructure` at project-create time and is the chart default. If it's missing, the project wasn't created with that flag; recreate the project:

```bash
diagrid project delete <name> --wait
diagrid project create <name> --region <region> \
  --enable-managed-workflow --enable-agent-infrastructure --use --wait
```

If you've explicitly overridden `stateStore.componentName` to something else (e.g. `workflowstatestore` for an upstream-Dapr deploy), confirm what's in the live project:

```bash
diagrid component list --project <your-project>
```

**Upstream Dapr (no Catalyst)** — flip the chart back to creating its own Postgres-backed component:

```bash
helm upgrade agent deploy/agent -n bank-heist --reuse-values \
  --set stateStore.create=true \
  --set stateStore.componentName=workflowstatestore
```

Confirm the Component CRD exists:

```bash
kubectl get crd | grep dapr.io          # CRDs installed? if not, dapr init -k --wait
kubectl -n bank-heist get components.dapr.io
```

## Public LB IP exists but external traffic times out

Symptom: `kubectl get svc <name>` shows an `EXTERNAL-IP`, but `curl http://<ip>/` from your laptop just hangs (`connect-timeout`). In-cluster probes (a `kubectl run` with curl) succeed.

This is an AKS-specific cloud-controller bug — the LB's Azure backend pool is empty. Verify:

```bash
NODE_RG=$(az aks show -g <rg> -n <cluster> --query nodeResourceGroup -o tsv)
az network lb address-pool show -g $NODE_RG --lb-name kubernetes \
  --name kubernetes -o json | jq '.loadBalancerBackendAddresses | length'
```

If that prints `0`, the pool wasn't populated. Fix by recreating the Service so AKS re-reconciles:

```bash
kubectl -n <ns> delete svc <svc-name>
helm upgrade --install <release> <chart> -n <ns>   # same args you originally used
```

Other things to check: node label `node.kubernetes.io/exclude-from-external-load-balancers` (remove if set), NSG inbound rule for port 80/443 on the LB's destination IP.

## `helm upgrade --reuse-values` keeps an old value

Surprising but by design: when a chart's default value changes between versions, `--reuse-values` preserves whatever the stored release had — including old defaults. The new default in the chart never wins unless you explicitly `--set`.

If a value should now be different than what was originally installed, override it:

```bash
helm upgrade <release> <chart> -n <ns> --reuse-values \
  --set service.port=80 \
  --set service.type=LoadBalancer
```

Nuclear option: `--reset-values` and re-pass all your env-specific flags from scratch.

## `helm upgrade` server-side-apply conflict

```
conflict occurred while applying object ... Apply failed with 1 conflict:
conflict with "kubectl-patch" using v1: .spec.type
```

Another field manager (usually `kubectl` from a previous `kubectl patch` or `kubectl scale`) owns the field Helm wants to set. Options:

- `helm upgrade ... --server-side=false` — fall back to client-side apply, which doesn't care about field ownership.
- Or align the chart values with the live state so there's no conflict (e.g. pass `--set replicaCount=$(kubectl get deploy ... -o jsonpath='{.spec.replicas}')`).

## `Error: UPGRADE FAILED: nil pointer evaluating interface {}.<field>`

Chart added a new values block (`catalyst`, `topologySpread`, etc.) but the stored release's values predate it. `--reuse-values` carries forward the old map, the template hits the missing key, dies.

Either:
- The template should use nil-safe access: `(.Values.foo).enabled` instead of `.Values.foo.enabled`. Already applied for the demo's `catalyst` and `topologySpread` blocks.
- Or `helm upgrade --reset-values` and re-pass everything.

## `MCP_URL` returning 421 / requests hanging

Two likely causes:

1. **Missing trailing slash.** FastMCP redirects `/mcp` → `/mcp/` and the streamable-http client doesn't follow POST redirects. Always end the URL in `/mcp/`.

2. **Wrong port.** The MCP Service is now on port 80 by default (`deploy/mcp/values.yaml`). If a stored release still has `mcp.url=http://mcp...:8000/mcp/` and you changed the Service to 80, callers fail. Fix:
   ```bash
   helm upgrade agent deploy/agent -n bank-heist --reuse-values \
     --set mcp.url=http://mcp.bank-heist.svc.cluster.local/mcp/ \
     --set mcp.httpBase=http://mcp.bank-heist.svc.cluster.local
   ```

## New image pushed but pods still on the old code

`imagePullPolicy: IfNotPresent` (Kubernetes default) plus the same image tag means the node uses its cached image regardless of what's now in the registry. The MCP/agent charts set `pullPolicy: Always` by default — verify:

```bash
kubectl -n bank-heist get deploy mcp -o yaml | grep -A1 imagePullPolicy
```

If it's `IfNotPresent`, fix:

```bash
kubectl -n bank-heist patch deployment mcp -p \
  '{"spec":{"template":{"spec":{"containers":[{"name":"mcp","imagePullPolicy":"Always"}]}}}}'
kubectl -n bank-heist rollout restart deployment/mcp
```

## Throughput notes by deploy mode

- **Catalyst Self-Hosted** (data plane in your cluster): no per-app RPS limit, intra-cluster latency. The default `target_concurrency` of 100 works fine.
- **Local Dapr** (sidecar in-pod): fastest path; activities never leave the pod.
- **Local Catalyst** (`diagrid dev run`): all traffic tunnels through the dev proxy; expect slower throughput than Self-Hosted but the durability story is identical.

The durability story (the actual point of the demo) works at any throughput.

## `kubectl set env … VAR-` leaves the env on the pod

`kubectl set env … VAR-` (trailing dash) removes the var from the deployment spec, but if pods were already running with it set, you still need a `rollout restart` for the new spec to take effect:

```bash
kubectl -n bank-heist set env deployment/agent FORCE_WORKFLOW_NAME-
kubectl -n bank-heist rollout restart deployment/agent
kubectl -n bank-heist exec deploy/agent -c agent -- printenv | grep FORCE_WORKFLOW_NAME
# Should print nothing.
```

## Workflows show in Catalyst console but are scheduled with the wrong name

If Catalyst's workflow list shows entries named `agent_workflow` but they fail with `OrchestratorNotRegisteredError`, the agent code is using the wrong schedule name for that environment.

The demo unifies on `dapr.agents.banker.workflow` (lowercase, matches what dapr-agents 1.x registers). The `FORCE_WORKFLOW_NAME` env var is the per-deployment override if a runtime needs the short alias (currently only Catalyst Local under `diagrid dev run` has historically required it).

If `FORCE_WORKFLOW_NAME` is unset and you're still seeing `agent_workflow` in the console, the running pod is on a stale image — rebuild and force-pull.
