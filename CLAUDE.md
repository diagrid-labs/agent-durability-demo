# Bank Heist Demo

Diagrid sales demo: 100 Dapr durable workflows credit 10 customer accounts $100 → $200 ($1 per tx) while chaos is injected. Invariant: every account finishes at $200, tx count = 1000, zero loss. **Sales demo, not production code.** Bar is "UI works cleanly on stage."

## Architecture map

```
ui-prototype/                 React UI, Babel-in-browser (no build pipeline)
  index.html                  entry — also served from MCP at /
  src/telemetry.jsx           createTelemetry — polls MCP, projects into UI state
  src/shell.jsx               TopBar, Counters, mode toggle (see SHOW_MODE_TOGGLE)
  src/grids.jsx               AgentsGrid, CustomersGrid
  src/panels.jsx              McpServer, ChaosPanel, PodFleet, Sidebar
  tweaks-panel.jsx            dev tweaks panel

services/mcp/mcp_server/      FastAPI host (port 8000 → host 9000)
  server.py                   FastAPI app, routes, MCP log ring, static UI mount
  orchestrator.py             in-memory work queue (1000 tasks default)
  replenisher.py              spawns workflows via agent's /schedule-one  ← THE driver
  pod_chaos.py                real k8s API pod kills (PodChaosController)
  slot_tracker.py             maps pods ↔ workflow slots for the heatmap
  db.py                       asyncpg pool, accounts/transactions/execution_runs
  chaos.py                    drop/latency injection inside MCP tools

services/agent/agent_worker/  Dapr workflow worker
  main.py                     FastAPI + WorkflowRuntime; /schedule-one is hot path
  agent.py                    DurableAgent ("banker") + tool defs
  stub_llm.py                 deterministic stub for STUB_LLM=true
  mcp_client.py               MCP streamable-http client

deploy/                       Helm charts: agent, mcp, postgres, ingress
local/                        compose.yaml + init.sql for laptop-only loop
```

## Data flow (Start run click)

```
UI Start ─POST→ MCP /agent/spawn ─→ Replenisher.start
                                    └→ orch.reset() — fills 1000-task queue
                                    └→ loop: for slot in N: POST agent /schedule-one
                                                           └→ DaprWorkflowClient.schedule_new_workflow
                                                                  → Catalyst workflow engine
                                                                       → durable activities call MCP tools
                                                                            → Postgres credit_account (idempotent)
```

## Bring-up (canonical)

Production (AKS + Catalyst remote): Helm charts in `deploy/`, see `CATALYST_CLOUD.md` + `AKS.md`.
Local: `docker compose -f local/compose.yaml up -d --build` + `diagrid dev run --file dapr.yaml --project <p>` per `CATALYST.md`.
UI: `http://<host>/index.html` (or `localhost:9000` locally — same origin, no CORS).

## Critical gotchas (read before debugging)

**`DurableAgent` workflows cannot be terminated via the management API.** UI tooltip: "Workflow management APIs not supported on DurableAgent workflows." `diagrid workflow terminate/pause/purge` silently fail. Only cleanup path is wiping the `agent-workflow` state store (`diagrid component delete agent-workflow`) or recreating the app-id. See `TROUBLESHOOTING.md`.

**Catalyst has a per-app-id RPS rate limit.** The replenisher bursts up to ~200 schedule calls/sec and running workflows do `GetState`/`PutState` on top. Manifestations: `RESOURCE_EXHAUSTED ... grpc_ratelimit middleware` (explicit) or `UNAVAILABLE: Socket closed` (LB drops). Throttled via `SCHEDULE_THROTTLE_MS` env on MCP (default 50ms ≈ 20 schedules/sec). Lower `target_concurrency` if you still see drops at scale.

**`MCP_URL` must use the Service port, not the container port.** Service runs on port 80, container listens on 8000. `MCP_URL=http://mcp.bank-heist.svc.cluster.local:8000/mcp/` (with explicit `:8000`) → silent `ConnectTimeout` on every call because nothing listens on the Service at port 8000. Correct: `http://mcp.bank-heist.svc.cluster.local/mcp/` (port-less). Easy to get wrong via stale `--set mcp.url=…` overrides from local-compose where the host-mapped port WAS 8000/9000.

**Workflow name is registered lowercase, not PascalCase.** dapr-agents 1.x registers as `dapr.agents.<name-lower>.workflow` (e.g. `dapr.agents.banker.workflow`), matching the activity naming. The PascalCase name `dapr.agents.Banker.workflow` will be silently rejected by Catalyst with "orchestrator was not registered" and the workflow FAILs in <1s. The `schedule_one` / `trigger` / `_schedule_one` defaults in `main.py` now use lowercase; override via `FORCE_WORKFLOW_NAME` env if the agent's `name=` kwarg changes.

**`mcp_queries` undercounts in `single` mode.** The `log_mcp("req"/"res")` calls live inside the per-tool wrappers (`list_customers`, `credit_account`) — not inside `process_task` (the atomic single-mode tool). So when running in `single` mode, the MCP-server card and `mcp_queries` counter look empty even though credits are landing in Postgres. Add `log_mcp` calls inside `process_task` if you need that visibility.

**The replenisher lives on the MCP server, not the agent.** `services/agent/agent_worker/main.py` still has legacy `/spawn-agents` + `_replenish_loop` code that's NOT the active path — ignore it when debugging. The live path is `Replenisher` in `services/mcp/mcp_server/replenisher.py` calling agent's `/schedule-one` (stateless, line ~100 of `main.py`).

**Pre-bank-agent-creditor workflows registered as `agent_workflow` are zombies.** Old runs under the previous app-id are stuck with a different workflow name than current workers register, so they won't be picked up. They live in the auto-provisioned `agent-workflow` state component, which is *managed* — `diagrid component delete` rejects with "managed diagrid components cannot be deleted directly." Cleanup paths: **(a)** wipe the state store directly via the **Catalyst Console UI** (Components → agent-workflow → clear/reset — works even when the CLI refuses), **(b)** delete + recreate the app-id, or **(c)** recreate the whole Catalyst project.

**State-store component name is `agent-memory`** (auto-provisioned by Catalyst `--enable-agent-infrastructure`). The agent chart now defaults to this (`stateStore.componentName: agent-memory`, `stateStore.create: false`). If you re-introduce a local-mode chart deploy, override back to `workflowstatestore` + `create: true`.

**Catalyst remote mode = no daprd sidecar.** Don't grep for or expect a `daprd` container in agent pods. The Dapr SDK reads `DAPR_HTTP_ENDPOINT`/`DAPR_GRPC_ENDPOINT`/`DAPR_API_TOKEN` directly from env. Set `dapr.enabled=false` in Helm values; the chart conditionally injects those env vars when `catalyst.enabled=true`.

**Catalyst Self-Hosted endpoint hostnames are project-scoped, not app-scoped.** Cloud Catalyst returns per-app URLs in `diagrid appid get`; Self-Hosted returns `null` and uses pattern `http-prj<id>.<region>` / `grpc-prj<id>.<region>` for HTTP/gRPC respectively (Envoy routes by `dapr-api-token` header to identify the appid). Find the project id from `diagrid project get`; find the region wildcard from `diagrid region get <region>` (look for `ingress.wildcardDomain`). Find the right endpoint by port-forwarding the gateway-envoy pod's admin port (9090) and dumping `/config_dump` → `virtual_hosts[].domains`.

**Catalyst Self-Hosted gateway LB exposes ports 8080/8443 by default**, but the `diagrid` CLI hardcodes `:443` for management API calls. Symptom: CLI from laptop times out with `i/o timeout` to LB external IP. Fix is a chart values override:
```yaml
gateway:
  envoy:
    service:
      port: 80
      httpsPort: 443
```
Saved in `deploy/catalyst-selfhosted/port-override.yaml`. Apply with `diagrid region deploy <region> --values-file ...` (in-place upgrade, LB IP usually persists). See chart default: https://github.com/diagridio/charts/blob/main/charts/catalyst/values.yaml#L809-L815.

**AKS hairpin NAT blocks pod → own-cluster-LB traffic.** When Catalyst Self-Hosted runs in the same cluster as the apps, pods can't reach the Catalyst gateway via its public LoadBalancer IP (TCP connect times out). Workaround: agent chart's `catalyst.hostAliases` (in `values.yaml`) maps the public Catalyst hostnames to the gateway-envoy ClusterIP so connections stay in-cluster. The chart writes this to `spec.template.spec.hostAliases` in the agent Deployment — survives `helm upgrade`. Without this, agent workflow runtime gets `Connection timed out` retrying the LB external IP forever.

**UI mode is hardcoded `single` and the toggle is hidden.** `ui-prototype/src/shell.jsx:35`: `const SHOW_MODE_TOGGLE = false;` and `useState('single')`. To switch to `multi`, flip the constant or change the default — there's no runtime switch in the UI today.

**`AGENT_HTTP_BASE` env var on the MCP server** determines where the replenisher POSTs `/schedule-one`. In k8s set to `http://agent.<ns>.svc.cluster.local:8000`; locally `http://host.docker.internal:8000`. The default baked into the code is the local one — k8s deployments **must** override via Helm.

**Demo is single-source-of-truth in Postgres.** Customer balance correctness comes from `accounts.balance`. `transactions.tx_id` has UNIQUE constraint with `ON CONFLICT DO NOTHING` — that's the idempotency gate. Don't TRUNCATE between runs unless you also reset balances to 100.

## Where to look for…

| If you need… | Look at… |
|---|---|
| Why workflows aren't starting | MCP pod logs (replenisher), then agent pod logs (`Starting new …` repeating = retry storm = downstream failure) |
| Per-agent / per-pod state in UI | `state.run`, `state.chaos.pods` in `ui-prototype/src/telemetry.jsx` |
| MCP tool definitions | `@mcp.tool()` decorators in `services/mcp/mcp_server/server.py` |
| Real pod-chaos endpoints | `/chaos/pods`, `/chaos/pod-kill`, `/chaos/az-kill` → `pod_chaos.py` |
| Catalyst bring-up + provisioning | `CATALYST_CLOUD.md`, `CATALYST.md` |
| AKS topology (3 nodepools, zone spread) | `AKS.md`, `deploy/agent/values.yaml:topologySpread` |
| Common operational failures + fixes | `TROUBLESHOOTING.md` |

## Code style for this repo

- Demo code: no tests, no abstractions beyond what's needed, no comments unless WHY is non-obvious.
- UI is Babel-in-browser; no JSX transpile step, no module bundler. Don't add one.
- Postgres password `bankadmin` is hardcoded — demo-only credential, do not flag as a security finding.
- Don't refactor on the side of bug fixes. Three similar lines beats a premature abstraction.

## Sister persisted memory

Strategic context lives in auto-memory (`~/.claude/projects/-Users-martezkillens-src-agent-durability-demo/memory/`): demo constraints, framing rules ("credit" not "drain"), Catalyst port checklist, future-state roadmap. This file is for codebase orientation only.
