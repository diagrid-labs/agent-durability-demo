# Bank Creditor Demo

Diagrid sales demo: 10 long-running Dapr durable workflows, one permanently bound to one customer account each, credit that account $100 → $200 in $1 increments (100 credits/account, 1000 tx total) while chaos is injected. Invariant: every account finishes at $200, tx count = 1000, zero loss. The point of one-instance-per-account (as opposed to the earlier ~100-short-lived-workflows design) is that a pod kill mid-run visibly freezes one account's balance at an interior value and — for activities not caught mid-flight on the killed pod — resumes from there, not from $100, demonstrating durable execution live. **Sales demo, not production code.** Bar is "UI works cleanly on stage."

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

services/agent/agent_worker/  Dapr workflow worker (LangGraph via diagrid.agent.langgraph)
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

**`diagrid workflow terminate/pause/purge` are silently no-op for these workflows — but `main.py`'s own endpoints work.** The CLI hits Catalyst's management API which marks the workflow terminated in Catalyst's metadata but doesn't propagate the signal to the underlying durabletask runtime. The workflow runs to natural completion and the COMPLETED state overwrites the TERMINATED marker. Symptom: CLI returns `Status: success`, but `diagrid workflow get` later shows `status: completed` with a full natural execution history.

The actual fix path is `/agent/instances/{instance_id}/terminate` and `/purge` in `services/agent/agent_worker/main.py`, which call `runner.terminate_workflow()` / `runner.purge_workflow()` — plain methods on `DaprWorkflowGraphRunner`'s `BaseWorkflowRunner` base (`diagrid.agent.core.workflow.runner`) that wrap `DaprWorkflowClient.terminate_workflow()`/`.purge_workflow()` over gRPC directly to the durabletask runtime, actually stopping the workflow at the next activity boundary. For bulk cleanup of stuck instances, wipe the `agent-workflow` state store via the Catalyst Console UI or recreate the app-id.

**Catalyst has a per-app-id RPS rate limit.** Historically (the ~100-short-lived-workflows design) the replenisher bursted up to ~200 schedule calls/sec; the one-instance-per-account redesign only ever issues 10 `/schedule-one` calls at run start, so this is far less likely to bite now — but the 10 running workflows still do `GetState`/`PutState` on top continuously. Manifestations: `RESOURCE_EXHAUSTED ... grpc_ratelimit middleware` (explicit) or `UNAVAILABLE: Socket closed` (LB drops). Throttled via `SCHEDULE_THROTTLE_MS` env on MCP (default 50ms).

**The agent no longer calls the `mcp` Service directly — everything routes through Catalyst's MCP proxy.** `mcp_client.py` talks to `$DAPR_HTTP_ENDPOINT/v1.0/diagrid/mcp/$MCP_SERVER_NAME` (the `mcp` Service is registered as a Catalyst `MCPServer` resource; see `docs/CATALYST_SELF_HOSTED.md` step 8). New MCP servers deny every tool until granted — `403 Forbidden` almost always means the access grant is missing or stale, not a network problem. See `docs/CATALYST_SELF_HOSTED.md`'s "Common failure modes" for the `405`-from-trailing-slash-stripping gotcha this uncovered in FastMCP's `/mcp` mount, fixed by `_MCPTrailingSlashMiddleware` in `server.py`.

**Workflow name convention: `dapr.<framework>.<TitleCaseName>.workflow`.** `diagrid.agent.langgraph`'s `DaprWorkflowGraphRunner` registers as `dapr.<framework>.<TitleCaseName>.workflow` — for this demo, `dapr.langgraph.Banker.workflow` (`diagrid.agent.core.workflow.naming.sanitize_agent_name` TitleCases the `name="banker"` kwarg; framework segment stays lowercase). `main.py` never needs to know or construct this name string itself — `/schedule-one` and `/trigger` call `runner.run_async(..., workflow_id=instance_id)` directly, which schedules the correct registered workflow function internally.

**`diagrid.agent.langgraph`'s node registry only extracts a node's *sync* callable — `async def` graph nodes silently fail to register.** `DaprWorkflowGraphRunner._register_graph_components()` (as of `diagrid[langgraph]==0.4.2`) pulls `node_spec.bound.func` to find each node's callable; for an `async def` node, LangGraph's `RunnableCallable.func` is `None` (the coroutine function lives at `.afunc` instead), so registration silently no-ops and logs `Could not extract callable for node: <name>` — the workflow then fails at that node with "not found in registry". Fix: register plain `def` node functions that return the (unawaited) coroutine from an inner `async def` implementation, e.g. `def call_tools(state): return _call_tools_impl(state)`. This keeps LangGraph's sync/async node-type detection on the sync path (so `.func` gets set) while still giving the Dapr activity executor a coroutine — `execute_node_activity`'s `_run_node()` already has an `asyncio.iscoroutine(result)` branch that awaits it correctly. See `services/agent/agent_worker/agent.py`.

**`DaprWorkflowGraphRunner(max_steps=...)` defaults to 100 — a hard cap on total graph steps (node invocations, not credits) for one workflow instance.** Each credit costs one (agent, tools) node-pair = 2 graph steps (decide, `credit_next`), so 100 credits × 2 steps = 200. Sized to `max_steps=400` for headroom; rescale if `credits_per_customer` changes. (Earlier — before consolidating to one MCP call per credit, see below — each credit cost 8 steps and the default 100-step cap silently truncated every instance at ~12 credits with no error, just `diagrid workflow list` showing `status: completed` way short of $200.)

**Don't use LangGraph's `MessagesState` (an accumulating chat transcript) for a long-running instance — it hits Catalyst's gRPC payload ceiling.** `DaprWorkflowGraphRunner` retransmits the *entire* workflow state over gRPC on every single activity call, so a message list that grows every credit will eventually blow past Catalyst's `max gRPC body size` (~4MB) — confirmed via `diagrid workflow get <id> --app-id bank-agent-creditor`, which showed `status: stalled` with `ExecutionStalled` / *"Workflow payload size ... exceeds 95% of max gRPC body size 4194304 bytes"*. Fixed by replacing `MessagesState` with `BankerState`, a fixed-size `TypedDict` (`requester`, `customer_id`, `last_result`, `done`, `final_message`) whose fields get *overwritten* each step instead of appended — payload size stays flat regardless of credit count. See `stub_llm.py`'s module docstring and `agent.py`'s `BankerState`. Side effect: real-mode (`STUB_LLM=false`, a real chat model) is no longer supported under this state shape — there's no transcript for an LLM to reason over; `_build_model()` now raises rather than silently misbehaving if that env var is flipped.

**MCP tool surface is one call per credit (`credit_next`), not four.** Originally each credit cycle made 4 separate MCP round trips (`get_next_task` → `get_balance` → `credit_account` → `report_done`) — 4x the MCP traffic/log noise for no benefit once the whole cycle lives server-side anyway. `server.py`'s `credit_next(requester, customer_id, pod)` tool now does all four steps (claim task, check balance, credit if needed, mark done) in one call, using `Orchestrator.next_task()`/`report_done()` and `Database.get_balance()`/`credit_account()` directly as plain Python rather than round-tripping through MCP for each. `agent.py`/`stub_llm.py` are simpler to match — `TOOLS` is just `[credit_next]`.

**A pod killed exactly while it's executing an activity may not resume — even after restarting all agent pods.** Confirmed live: killing a pod mid-credit, some of that pod's workflows recovered and finished normally (their next activity landed on the surviving pod), but others stayed frozen at their exact kill-time balance for 10+ minutes with zero recovery, even after a full `kubectl rollout restart deployment/agent`. `diagrid workflow get` showed the interrupted activity's `TaskScheduled` event with no matching `TaskCompleted` ever arriving, while Catalyst still reported the workflow as `status: running` (not an error state) — the abandoned work item just never got redispatched to another worker within any timeframe tested. `diagrid.agent.langgraph`'s runner schedules activities with no retry policy, so recovery depends entirely on a Catalyst-side lock/visibility timeout that didn't fire. Net effect: pod-kill chaos reliably freezes an account's balance, but "resumes automatically" only held for instances *not* caught mid-activity at the exact moment of the kill — a known limitation, not yet root-caused further (worth revisiting: an explicit Dapr activity retry policy, or a Catalyst-side visibility-timeout setting).

**`DaprWorkflowGraphRunner` builds its own internal `WorkflowRuntime(host=host, port=port)` with no concurrency knobs exposed** — there's no `AGENT_STATE_STORE` env var either; LangGraph durability comes entirely from Dapr Workflow activity persistence, not a separate chat-memory state store. `stateStore.componentName` in `deploy/agent/values.yaml` still matters, though — it names the CRD `templates/component-state.yaml` renders when `stateStore.create: true` (non-Catalyst chart deploy).

**The replenisher lives on the MCP server, not the agent.** `Replenisher` in `services/mcp/mcp_server/replenisher.py` owns the loop and calls agent's `/schedule-one` (stateless, line ~100 of `main.py`).

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

**`AGENT_HTTP_BASE` env var on the MCP server** determines where the replenisher POSTs `/schedule-one`. In k8s set to `http://agent.<ns>.svc.cluster.local:8000`; locally `http://host.docker.internal:8000`. The default baked into the code is the local one — k8s deployments **must** override via Helm.

**Demo is single-source-of-truth in Postgres.** Customer balance correctness comes from `accounts.balance`. `transactions.tx_id` has UNIQUE constraint with `ON CONFLICT DO NOTHING` — that's the idempotency gate. Don't TRUNCATE between runs unless you also reset balances to 100.

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

## Code style for this repo

- Demo code: no tests, no abstractions beyond what's needed, no comments unless WHY is non-obvious.
- UI is Babel-in-browser; no JSX transpile step, no module bundler. Don't add one.
- Postgres password `bankadmin` is hardcoded — demo-only credential, do not flag as a security finding.
- Don't refactor on the side of bug fixes. Three similar lines beats a premature abstraction.

## Sister persisted memory

Strategic context lives in auto-memory (`~/.claude/projects/-Users-martezkillens-src-agent-durability-demo/memory/`): demo constraints, framing rules ("credit" not "drain"), Catalyst port checklist, future-state roadmap. This file is for codebase orientation only.
