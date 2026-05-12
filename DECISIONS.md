# Decisions

ADR-lite log of substantive decisions made for the Bank Heist demo. Newest at the bottom. Each entry lists the choice, the reason, what we passed on, and what it commits us to.

---

## 1. Serve the UI over HTTP, not `file://`
**Date:** 2026-05-05

**Decision:** UI is served by a local HTTP server (`python3 -m http.server 8000`); never opened directly via `file://`.

**Context:** The HTML uses `<script type="text/babel" src="...jsx">` tags. Babel-standalone fetches each `.jsx` file via XHR. Browsers block those fetches over `file://` for CORS reasons; the page renders blank.

**Alternatives:** Inline all JSX into the HTML (loses module separation); pre-build with Vite/esbuild (adds toolchain we don't need yet).

**Consequences:** Demo always boots through a static server. README documents this; later steps will likely embed UI hosting in the orchestrator pod.

---

## 2. Stack: Python backend + existing React UI
**Date:** 2026-05-05

**Decision:** Python for everything new (orchestrator, MCP server, agents). React UI kept as-is.

**Context:** User specified Python. UI is already built and uses a `createTelemetry` simulator with a clean state-shape contract that's easy to feed from any backend.

**Alternatives:** Go/TypeScript backends (better Dapr ergonomics in some places); rewriting UI in Next.js (no benefit, lots of work).

**Consequences:** Must use the Python `dapr-agents` and `mcp` SDKs. UI components are a fixed contract — backend events must conform to the existing `state` shape.

---

## 3. Kubernetes-native deployment
**Date:** 2026-05-05

**Decision:** All components run on Kubernetes. No docker-compose path.

**Context:** User specified k8s. The chaos buttons are most credible when they map to real k8s primitives (`kubectl delete pod`, zone cordon).

**Alternatives:** docker-compose for local dev (faster iteration, but the AZ-outage story dies).

**Consequences:** Every component ships as a Helm chart. Local development uses kind. CI will need a k8s cluster (kind in CI is fine).

---

## 4. Multi-node kind cluster with 3 chaos zones + 1 control zone
**Date:** 2026-05-05

**Decision:** 5 nodes: 1 k8s control-plane + 4 workers labeled `topology.kubernetes.io/zone={control,a,b,c}`.

**Context:** "AZ outage" needs a real failure domain to be credible. Single-node forces a fake (flag-toggle), which undercuts the entire durability narrative.

**Alternatives:** Single-node (rejected — kills the marquee scene); 3 nodes without a control zone (chaos can hit Postgres, breaks the source of truth); GKE/EKS (overkill for a demo).

**Consequences:** ~3x resource cost vs single-node. Docker Desktop needs ≥6 GB RAM. Bring-up is ~2-3 min. AZ outage demo is real, not faked.

---

## 5. 100 agents implemented as 100 workflow instances on a shared worker pool
**Date:** 2026-05-05

**Decision:** 100 Dapr workflow instances run on ~15 worker pods (5 per chaos zone). Each UI heatmap cell = one workflow instance.

**Context:** 100 agents is a hard requirement. Running 100 pods + 100 Dapr sidecars (200 containers) is heavy on a laptop. Dapr's "agent" is a workflow instance, not a pod — the runtime separates them.

**Alternatives:** 100 pods (1 agent per pod) — more visceral, much heavier. Kept as a Helm value toggle for later.

**Consequences:** "Kill agent N" maps to terminating workflow N or killing the pod currently hosting it; either way, surviving workers pick the workflow up. Telemetry layer must map workflow instance ID ↔ heatmap cell ID.

---

## 6. LLM stubbed by default, real-model behind a flag
**Date:** 2026-05-05

**Decision:** Default mode is a deterministic Python stub for the agent's "decide which customer to credit" step. Helm value `agent.llm.mode=real` (or `--real-model` CLI) enables a live model.

**Context:** The demo's invariant is balance arithmetic, not language generation. A real LLM adds non-determinism, latency, cost, and a network dependency. User flagged that they may flip to always-on later.

**Alternatives:** Always-on real model (fragile demo); always-stub (kills the "look, it's an LLM agent" moment).

**Consequences:** Agent code has two code paths. Both must produce the same observable behavior (pick a not-done customer, call `credit_account`). Test invariant in both modes.

---

## 7. Single Postgres instance, two databases
**Date:** 2026-05-05

**Decision:** One Postgres pod, two logical databases: `bankdemo` (app data) and `dapr_state` (Dapr workflow checkpoints).

**Context:** Need a place for Dapr to persist workflow state so workflows survive pod kills. Could be Redis/etcd/Postgres. Postgres is already there.

**Alternatives:** Separate Postgres for Dapr (more pods, more failure modes); Redis for Dapr state (extra component); same database different schemas (less isolation).

**Consequences:** MCP server's connection string only points at `bankdemo` — it physically cannot corrupt workflow state. Single Postgres is a SPOF, but it lives in the never-attacked control zone, so the demo invariant holds.

---

## 8. Idempotency via deterministic `tx_id` + `ON CONFLICT DO NOTHING`
**Date:** 2026-05-05

**Decision:** Every credit carries `tx_id = wf-{workflow_instance_id}-step-{n}`. The MCP server's `credit_account` does `INSERT … ON CONFLICT (tx_id) DO NOTHING` then conditionally updates the balance, in a single SQL transaction.

**Context:** Dapr workflows replay steps after pod kill. Without an idempotency key, replay double-credits. The demo invariant (`COUNT(transactions) = 1000`) only holds if duplicates are rejected at the DB.

**Alternatives:** Idempotency in the MCP server's memory (lost on restart); UUID-per-call (not deterministic, replays generate new IDs and double-credit); two-phase commit / sagas (huge overkill).

**Consequences:** Anything mutating `accounts.balance` must go through `credit_account` with a deterministic key. Direct UPDATEs are forbidden. New mutating tools (debit, transfer) must follow the same pattern.

---

## 9. `pg_notify` trigger drives the UI feed; no polling
**Date:** 2026-05-05

**Decision:** A trigger on `transactions` insert fires `pg_notify('tx_committed', json_payload)`. The orchestrator subscribes via `LISTEN` and forwards to the UI WebSocket.

**Context:** UI needs sub-second tx visibility for 100 agents × 1 tx/tick. Polling at that rate hammers the DB; logical replication is overkill.

**Alternatives:** Polling `transactions` (extra load, latency, missed events); CDC via Debezium (heavy infra); have the MCP server emit events directly (then a missed event = a dropped UI update — DB-level guarantees the trigger fires inside the commit).

**Consequences:** UI events are guaranteed-on-commit. Orchestrator must hold a `LISTEN` connection; on reconnect it can rebuild state from `SELECT MAX(created_at)`.

---

## 10. Custom minimal Helm chart for Postgres (no Bitnami)
**Date:** 2026-05-05

**Decision:** Custom 6-file chart at `deploy/postgres/`.

**Context:** Bitnami's chart is excellent but huge — hundreds of values, security primitives we don't need, a learning surface that hides what's actually deployed.

**Alternatives:** Bitnami chart (production-ready, opaque); raw `kubectl apply` (no templating).

**Consequences:** We own the chart. Adding HA / replication later is our problem, not a values flip. Acceptable for a demo.

---

## 11. Postgres pinned to the control zone
**Date:** 2026-05-05

**Decision:** Postgres StatefulSet has `nodeSelector: topology.kubernetes.io/zone=control`.

**Context:** Chaos buttons target zones a/b/c. If Postgres is in a chaos zone, AZ outage takes down the source of truth and the demo invariant is unverifiable.

**Alternatives:** Let scheduler choose (Postgres lands wherever; chaos may kill it).

**Consequences:** Same rule applies to MCP server, orchestrator, Dapr placement. Anything stateful or "control plane" goes to control zone. Anything that's a chaos target goes to a/b/c.

---

## 12. Seed customer names match the UI's deterministic `nameFor(0..9)`
**Date:** 2026-05-05

**Decision:** Database seed contains the exact 10 names that `ui-prototype/src/telemetry.jsx:nameFor(id)` produces for ids 0–9.

**Context:** The UI today computes names client-side. When the simulator is replaced with the real backend, the heatmap labels would change unless the DB matches.

**Alternatives:** Pick fresh names (forces UI label changes); push names from UI to DB at startup (over-engineered).

**Consequences:** The two definitions are now coupled. If UI's `FIRST_NAMES`/`LAST_NAMES` arrays change, regenerate the seed.

---

## 13. Custom MCP server (not the reference Postgres MCP)
**Date:** 2026-05-05

**Decision:** Build a thin Python MCP server exposing exactly four tools (`list_customers`, `get_customer`, `credit_account`, `get_balance`).

**Context:** The official `@modelcontextprotocol/server-postgres` is read-only. Other community Postgres MCP servers expose generic `execute(sql)`, which would push the idempotency contract into agent prompts — fragile and undermines the demo claim.

**Alternatives:** Wrap a generic SQL-execute MCP and constrain via prompt (fragile); skip MCP entirely, have agents talk to Postgres directly (loses the "agents-via-MCP" story).

**Consequences:** ~150 lines of Python. We own the tool surface. Idempotency contract is enforced in code, not in prompts.

---

## 14. MCP transport: streamable HTTP (not stdio)
**Date:** 2026-05-05

**Decision:** MCP server runs as a long-lived service over HTTP. Agent pods connect via service DNS.

**Context:** stdio transport works for local-process tool servers. Inside k8s, multiple agent pods need to share one MCP server pod, which means a network transport.

**Alternatives:** stdio with a sidecar pattern (one MCP per agent — wastes resources); SSE (older MCP transport, being deprecated).

**Consequences:** MCP server is a Deployment with replicas, behind a Service. NetworkPolicy can restrict access later.

---

## 15. Chaos via MCP-server flags first; chaos-mesh deferred
**Date:** 2026-05-05

**Decision:** `dropTx` and `latencyJitter` are implemented as in-memory toggles on the MCP server. Pod-level chaos uses raw `kubectl`. Chaos-mesh is deferred to step 7+.

**Context:** Chaos-mesh is powerful but adds CRDs, a controller, and learning curve. For the demo's first pass, simple flags are honest representations of "the network dropped" or "the network slowed."

**Alternatives:** Chaos-mesh from day one (more realistic network chaos, much more setup).

**Consequences:** First version can't simulate true network partitions, only application-visible drops. Acceptable; revisit at step 7.

---

## 16. Single ASGI app exposes `/mcp` and `/chaos` on one port
**Date:** 2026-05-05

**Decision:** The MCP server process mounts both the MCP streamable-HTTP app at `/mcp` and a small FastAPI router at `/chaos` for orchestrator-only chaos toggles.

**Context:** Two surfaces (agent-facing MCP, orchestrator-facing chaos control) but they share state (the same `Chaos` and `Database` objects). One process is simpler than two.

**Alternatives:** Separate processes (clean isolation but cross-process state sync); chaos as an MCP tool too (agents could call it — fine for a demo, weird for prod).

**Consequences:** Path-based separation. NetworkPolicy will eventually restrict `/chaos` to orchestrator pods only.

---

## 17. ~~Step 3 uses raw Dapr Workflows, not `DurableAgent`~~ — REVERSED by #19
**Date:** 2026-05-05 · **Reversed:** 2026-05-05

Originally chose raw Dapr Workflows for step 3 to defer the LLM tool-loop ceremony. **Reversed by #19** in the same session: user wants `DurableAgent` framing from day one, with a stub LLM filling the LLM role until real-model mode is enabled. Original entry retained for history; see #19 for the active choice.

---

## 18. Workflow state store: inline credentials in the Dapr Component
**Date:** 2026-05-05

**Decision:** The `Component` manifest for `state.postgresql` writes the connection string with the password inline (no Dapr SecretStore reference).

**Context:** Dapr's k8s SecretStore + `secretKeyRef` requires the referenced Secret to expose a key formatted exactly as the Component expects. Our `postgres-credentials` Secret has separate `POSTGRES_USER`/`POSTGRES_PASSWORD` keys; reformatting it would couple the postgres chart to Dapr-specific layout.

**Alternatives:** Add a `connectionString` key to the postgres Secret (couples charts); use a Dapr SecretStore Component with key-mapping (more YAML, more moving parts).

**Consequences:** Password lives in the rendered Component manifest in the cluster (not in source — Helm value override). Acceptable for a demo running on kind. Real-world deploys would route through a SecretStore.

---

## 19. Use `DurableAgent` (not raw Dapr Workflows) with a stub LLM by default
**Date:** 2026-05-05

**Decision:** The agent worker uses `dapr_agents.DurableAgent`. A custom `StubLLM` class implements the LLM-client interface deterministically (tool-call sequence: `get_balance` → `credit_account` → `get_balance` → … until target). Real-LLM mode is a one-env-flag flip (`STUB_LLM=false`, `ANTHROPIC_API_KEY=...`) selected by the Helm value `agent.llm.mode`.

**Context:** Reverses #17. User wants the `DurableAgent` framing on the marquee from step 3, not deferred to step 9. The deterministic decisions are shifted into the LLM-stub class instead of being inlined in the workflow body.

**Alternatives:** Raw Dapr workflows (#17 — clearer durability story, no SDK surface concerns, but loses the "Dapr Agents" framing); `DurableAgent` with a real model from day one (loses determinism and adds cost/latency to every demo run).

**Consequences:**
- Replay determinism still works because `dapr-agents` checkpoints the LLM call as a workflow activity — the stub's output is cached, not re-derived, on replay.
- `tx_id`s are constructed by the stub LLM (`agent-c{customer}-call-{n}`) — deterministic within a single agent run. The DB's PK constraint is the actual idempotency gate; the stub's counter is just a unique-key generator.

**Implementation surface (`dapr-agents 1.0.1`, confirmed by inspection):**
- `DurableAgent` takes config dataclasses, not flat kwargs:
  - `profile=AgentProfileConfig(name=..., role=..., goal=..., instructions=[...])`
  - `state=AgentStateConfig(store=StateStoreService(store_name="workflowstatestore", key_prefix="banker-state"))`
  - `execution=AgentExecutionConfig(max_iterations=300)` — **default is 10**, must raise (~200 iterations needed to drain $100 → $200)
  - `llm=ChatClientBase`, `tools=[...]`
- `StubLLM` subclasses `dapr_agents.llm.chat.ChatClientBase`, implements `generate(messages, *, tools=None, ...) -> LLMChatResponse` and a no-op `from_prompty` classmethod.
- Real-mode uses `OpenAIChatClient` (default model `gpt-4o-mini`). dapr-agents 1.0.1 ships no Anthropic client; switching to Claude later requires writing a custom `ChatClientBase` subclass.

---

## 20. Install Dapr via the Dapr CLI (`dapr init -k`), not Helm
**Date:** 2026-05-05

**Decision:** The Dapr control plane is installed with `dapr init -k --wait` instead of the upstream Helm chart.

**Context:** The CLI wraps the Helm install with sensible defaults and gives us `dapr status -k`, `dapr workflow list`, and similar inspection commands "for free." Demo audiences are more likely to recognize the CLI flow than a raw Helm command.

**Alternatives:** Upstream `dapr/dapr` Helm chart (more declarative, but no extra inspection surface).

**Consequences:** Anyone reproducing the demo needs the Dapr CLI installed. Scriptable installs (CI) can use either path; the CLI is fine in CI too.
