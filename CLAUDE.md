# Bank Creditor Demo

Diagrid sales demo: 10 long-running Dapr durable workflows, one permanently bound to one customer account each, credit that account $100 → $200 in $1 increments (100 credits/account, 1000 tx total) while chaos is injected. Invariant: every account finishes at $200, tx count = 1000, zero loss. One instance per account means a pod kill mid-run visibly freezes that account's balance and resumes from there, demonstrating durable execution live. **Sales demo, not production code.** Bar is "UI works cleanly on stage."

## Architecture map

```
ui-prototype/                 React UI, Babel-in-browser (no build pipeline)
  index.html                  entry — also served from MCP at /
  src/telemetry.jsx           createTelemetry — polls MCP, projects into UI state
  src/shell.jsx               TopBar, Counters
  src/grids.jsx               AgentsGrid, CustomersGrid
  src/panels.jsx              McpServer, ChaosPanel, PodFleet, Sidebar
  tweaks-panel.jsx            dev tweaks panel

services/mcp/mcp_server/      FastAPI host (port 8000 → host 9000)
  server.py                   FastAPI app, routes, MCP log ring, static UI mount
  orchestrator.py             10 independent per-customer queues (100 tasks each)
  replenisher.py              spawns 10 instances once (one per customer) via agent's /schedule-one, then just watches for completion — no ongoing spawn loop
  pod_chaos.py                real k8s API pod kills (PodChaosController)
  slot_tracker.py             maps pods ↔ workflow slots for the heatmap
  db.py                       asyncpg pool, accounts/transactions/execution_runs
  chaos.py                    drop/latency injection inside MCP tools

services/agent-langgraph/agent_worker/  Dapr workflow worker (LangGraph via diagrid.agent.langgraph)
  main.py                     FastAPI + DaprWorkflowGraphRunner; /schedule-one is hot path
  agent.py                    LangGraph StateGraph ("banker") + DaprWorkflowGraphRunner + tool defs
  stub_llm.py                 deterministic stub for STUB_LLM=true
  mcp_client.py               MCP client — calls tools through Catalyst's MCP proxy, not the mcp Service directly

deploy/                       Helm charts: agent, mcp, postgres, ingress
local/                        compose.yaml + init.sql for laptop-only loop
```

## Data flow (Start run click)

```
UI Start ─POST→ MCP /agent/spawn ─→ Replenisher.start
                                    └→ orch.reset() — fills 1000-task queue
                                    └→ loop: for slot in N: POST agent /schedule-one
                                                           └→ DaprWorkflowGraphRunner.run_async(workflow_id=instance_id)
                                                                  → Catalyst workflow engine
                                                                       → durable activities execute LangGraph nodes, call MCP tools
                                                                            → Postgres credit_account (idempotent)
```

## Bring-up (canonical)

Production (any K8s + Catalyst Self-Hosted): Helm charts in `deploy/`, see `docs/CATALYST_SELF_HOSTED.md`.
Local: `docker compose -f local/compose.yaml up -d --build` + `diagrid dev run --file dapr.yaml --project <p>` per `docs/CATALYST.md`.
UI: `http://<host>/index.html` (or `localhost:9000` locally — same origin, no CORS).

## Critical gotchas (read before debugging)

**`diagrid workflow terminate/pause/purge` are silent no-ops for these workflows.** Use `/agent/instances/{id}/terminate` and `/purge` in `main.py` instead — they call `runner.terminate_workflow()`/`purge_workflow()` directly against the durabletask runtime. For bulk cleanup, wipe the `agent-workflow` state store via the Catalyst Console or recreate the app-id.

**Catalyst rate-limits per app-id.** Only 10 `/schedule-one` calls fire at run start, but the 10 running workflows still do continuous `GetState`/`PutState`. Watch for `RESOURCE_EXHAUSTED`/`UNAVAILABLE` errors; throttle via `SCHEDULE_THROTTLE_MS` on MCP (default 50ms).

**The agent talks to MCP only through Catalyst's MCP proxy**, never the `mcp` Service directly (see `docs/CATALYST_SELF_HOSTED.md` step 8). A `403` almost always means the tool's access grant is missing or stale, not a network problem.

**Workflow name convention: `dapr.<framework>.<TitleCaseName>.workflow`** (e.g. `dapr.langgraph.Banker.workflow`). `main.py` never builds this string itself — `run_async(workflow_id=...)` resolves the registered workflow internally.

**`diagrid.agent.langgraph` only registers a node's *sync* callable** — an `async def` graph node silently fails to register. Use a plain `def` wrapper that returns the coroutine unawaited; see `services/agent-langgraph/agent_worker/agent.py`.

**`DaprWorkflowGraphRunner(max_steps=...)` defaults to 100** — a cap on graph steps, not credits. Each credit costs 2 steps (decide, `credit_next`), so it's set to `max_steps=400`; rescale if `credits_per_customer` changes.

**Don't use LangGraph's `MessagesState` for a long-running instance** — it grows every step and blows Catalyst's ~4MB gRPC payload ceiling. Use a fixed-size `TypedDict` (`BankerState`) whose fields get overwritten each step instead. Side effect: real-mode (`STUB_LLM=false`) isn't supported under this state shape, since there's no transcript for a real model to reason over.

**MCP tool surface is one call per credit (`credit_next`).** `server.py`'s `credit_next(requester, customer_id, pod)` claims the task, checks balance, credits if needed, and marks done, all in one call.

**A pod killed mid-activity resumes on its own** — Catalyst redispatches the orphaned activity to a surviving worker. No sidecar-restart or other workaround needed; if a freeze is ever observed, treat it as a regression to report upstream rather than reintroducing one.

**`DaprWorkflowGraphRunner` builds its own `WorkflowRuntime` with no concurrency knobs exposed**, and there's no `AGENT_STATE_STORE` env var — durability comes entirely from Dapr Workflow activity persistence. `stateStore.componentName` in `deploy/agent/values.yaml` still matters for non-Catalyst chart deploys (`stateStore.create: true`).

**The replenisher lives on the MCP server, not the agent.** `Replenisher` in `services/mcp/mcp_server/replenisher.py` owns the loop and calls agent's `/schedule-one`.

**State-store component name is `agent-memory`**, auto-provisioned by Catalyst `--enable-agent-infrastructure`. The agent chart defaults `stateStore.componentName` to this with `create: false`.

**Catalyst remote mode has no `daprd` sidecar.** The Dapr SDK reads `DAPR_HTTP_ENDPOINT`/`DAPR_GRPC_ENDPOINT`/`DAPR_API_TOKEN` directly from env; set `dapr.enabled=false` in Helm values.

**Catalyst Self-Hosted endpoint hostnames are project-scoped, not app-scoped** — pattern `http-prj<id>.<region>` / `grpc-prj<id>.<region>`. Get the project id from `diagrid project get` and the region wildcard from `diagrid region get <region>` (`ingress.wildcardDomain`).

**Catalyst Self-Hosted's gateway LB exposes 8080/8443, but the `diagrid` CLI hardcodes `:443`.** Override via Helm values (saved in `deploy/catalyst-selfhosted/port-override.yaml`):
```yaml
gateway:
  envoy:
    service:
      port: 80
      httpsPort: 443
```

**AKS hairpin NAT blocks pod → own-cluster-LB traffic.** Fix: agent chart's `catalyst.hostAliases` maps Catalyst hostnames to the gateway-envoy ClusterIP so connections stay in-cluster.

**`AGENT_HTTP_BASE` env var on MCP** sets where the replenisher POSTs `/schedule-one` — override via Helm for k8s (`http://agent.<ns>.svc.cluster.local:8000`); the code default is local-only.

**Postgres is the source of truth.** `transactions.tx_id`'s UNIQUE constraint + `ON CONFLICT DO NOTHING` is the idempotency gate — don't `TRUNCATE` without also resetting balances to 100.

## Where to look for…

| If you need… | Look at… |
|---|---|
| Why workflows aren't starting | MCP pod logs (replenisher), then agent pod logs (`Starting new …` repeating = retry storm = downstream failure) |
| Per-agent / per-pod state in UI | `state.run`, `state.chaos.pods` in `ui-prototype/src/telemetry.jsx` |
| MCP tool definitions | `@mcp.tool()` decorators in `services/mcp/mcp_server/server.py` |
| Real pod-chaos endpoints | `/chaos/pods`, `/chaos/pod-kill`, `/chaos/az-kill` → `pod_chaos.py` |
| Catalyst bring-up + provisioning | `docs/CATALYST_SELF_HOSTED.md`, `docs/CATALYST.md` |
| Node topology (labels, zone spread) | `deploy/agent/values.yaml:topologySpread` |
| Common operational failures + fixes | `docs/TROUBLESHOOTING.md` |
| Testing Dapr Workflow behavior without Catalyst (debug-only) | `docs/LOCAL_DAPR.md` |

## Code style for this repo

- Demo code: no tests, no abstractions beyond what's needed, no comments unless WHY is non-obvious.
- UI is Babel-in-browser; no JSX transpile step, no module bundler. Don't add one.
- Postgres password `bankadmin` is hardcoded — demo-only credential, do not flag as a security finding.
- Don't refactor on the side of bug fixes. Three similar lines beats a premature abstraction.

## Sister persisted memory

Strategic context lives in auto-memory (`~/.claude/projects/-Users-martezkillens-src-agent-durability-demo/memory/`): demo constraints, framing rules ("credit" not "drain"), Catalyst port checklist, future-state roadmap. This file is for codebase orientation only.
