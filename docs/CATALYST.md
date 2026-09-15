# Bank Creditor on Catalyst — Local Mode

Run the Bank Creditor demo (a LangGraph agent, durably executed via `diagrid.agent.langgraph`) on your laptop with the agent's Dapr APIs proxied through **Diagrid Catalyst**. Workflow runtime, state store, and placement run in Catalyst; the agent process, MCP server, and bank Postgres run locally.

> For a k8s deploy that uses Catalyst Self-Hosted (no local `diagrid dev run`), see [CATALYST_SELF_HOSTED.md](./CATALYST_SELF_HOSTED.md) — that's also the canonical walkthrough for the `diagrid mcpserver` commands referenced below, since the agent's tool calls now go through Catalyst's MCP proxy rather than a direct connection.

## Architecture

```
┌────────────────────┐     diagrid dev tunnel    ┌─────────────────────────┐
│ Catalyst project   │ ◄───────────────────────► │ agent-worker (uvicorn)  │
│  · workflow engine │                           │  agent-langgraph        │
│  · state store     │                           │  appPort 8000           │
│  · MCP proxy        │──┐                        │  daprHTTPPort 3500      │
└────────────────────┘  │ /v1.0/diagrid/mcp/...   └────────────┬────────────┘
                         │                                     │ (tool calls route
                         ▼                                     │  through the MCP
              ┌─────────────────────────┐                      │  proxy, left)
              │ mcp — must be reachable │◄─────────────────────┘
              │ FROM Catalyst, not just │
              │ from this laptop        │
              └────────────┬────────────┘
                            │ asyncpg
                            ▼
              ┌─────────────────────────┐
              │ postgres (docker)       │
              │  localhost:5432         │
              └─────────────────────────┘
```

## Prerequisites

- Docker (or Podman) with `docker compose`
- Python ≥ 3.11 and [`uv`](https://docs.astral.sh/uv/)
- [Diagrid CLI](https://docs.diagrid.io/catalyst/references/cli-reference/intro)
- A Diagrid Catalyst account: <https://catalyst.diagrid.io>

## 1. Provision Catalyst resources

This demo needs both managed workflow **and** agent infrastructure at project-create time — they cannot be enabled on an existing project. `--enable-agent-infrastructure` provisions the `agent-memory` state store, but the agent doesn't read it directly — LangGraph durability comes entirely from Dapr Workflow activity persistence. This hasn't been re-verified against a project created with `--enable-managed-workflow` alone — keep passing both flags until that's confirmed.

```bash
diagrid login
diagrid project create my-project \
  -r diagrid-aws-eu-west \
  --enable-managed-workflow \
  --enable-agent-infrastructure \
  --use --wait
```

Create the agent's App ID:

```bash
diagrid appid create agent-worker --wait
```

`--enable-agent-infrastructure` auto-provisions a managed state store named `agent-memory`. Reuse that for the workflow state store — no extra `diagrid kv create` needed. Verify:

```bash
diagrid project get my-project   # ManagedWorkflowStore: enabled (+ agent infra)
diagrid component list                 # agent-memory state.diagrid all app identities ready
```

The agent doesn't read `agent-memory` directly — durability comes entirely from Dapr Workflow activity persistence in whatever store `--enable-managed-workflow` provisions. No env var needed for this.

## 2. Bring up local supporting services

Postgres (with bank schema + 10 seeded customers) and the MCP server run in docker:

```bash
docker compose -f local/compose.yaml up -d --build
```

Wait until both are healthy, then sanity-check:

```bash
docker compose -f local/compose.yaml ps
curl -s http://localhost:9000/healthz   # → {"status":"ok"}
```

### Register the MCP server with Catalyst

The agent's tool calls (`get_balance`, `credit_account`, `get_next_task`, `report_done`) go through Catalyst's managed MCP proxy — Catalyst has to be able to reach the MCP server's URL itself, not just your laptop.

```bash
diagrid mcpserver create bank-postgres-mcp \
  --project my-project \
  --url http://localhost:9000/mcp/ \
  --wait

diagrid mcpserver access grant bank-postgres-mcp \
  --project my-project \
  --caller agent-worker \
  --allow-tools get_balance,credit_account,get_next_task,report_done \
  --wait
```

**Known gap, not yet verified**: with Catalyst Cloud (this section's `my-project` project), `localhost:9000` is only reachable from *this laptop* — `diagrid dev run`'s tunnel exposes the agent's port outward, but doesn't expose the MCP server's port inward-to-Catalyst. If registration or tool calls fail with a connect error, you likely need to tunnel the MCP server too (e.g. `ngrok http 9000` and register that URL instead), or point this registration at an already-reachable MCP endpoint (like the one from [CATALYST_SELF_HOSTED.md](./CATALYST_SELF_HOSTED.md), if you have one). This was validated end-to-end for the Self-Hosted / in-cluster case, not for Catalyst Cloud + a laptop-only MCP server.

## 3. Install agent dependencies

The agent runs as a normal Python process (not in docker) so `diagrid dev run` can attach the daprd tunnel to it.

```bash
cd services/agent-langgraph
uv sync
cd -
```

## 4. Run the agent through Catalyst

From the repo root:

```bash
diagrid dev run --file dapr.yaml --project my-project \
  --skip-managed-kv --skip-managed-pubsub --skip-default-resiliency
```

The `--skip-*` flags prevent `dev run` from auto-creating duplicate default components on top of the ones agent-infrastructure already provisioned in §1.

This will:
1. Authenticate against your `my-project` project.
2. Open a dev tunnel from Catalyst back to `localhost:8000`.
3. Spawn `uvicorn agent_worker.main:app` and a local daprd that proxies all Dapr API calls to Catalyst.

You should see `runner started (stub=True)` once the FastAPI app finishes its `/v1.0/healthz/outbound` wait — preceded by `Registered node: agent` / `Registered node: tools` and `Registering workflow 'dapr.langgraph.Banker.workflow' with runtime`.

## 5. Trigger a workflow

In a second terminal:

```bash
curl -s -X POST localhost:8000/trigger \
  -H 'Content-Type: application/json' \
  -d '{"customer_id": 7, "target": 200}' | jq
```

Watch progress:

```bash
watch -n 1 'curl -s localhost:8000/status/customer-7 | jq ".runtime_status, .last_updated_at"'

# Bank-side ledger:
docker exec -it $(docker compose -f local/compose.yaml ps -q postgres) \
  psql -U bankadmin -d bankdemo -c \
  "SELECT customer_id, count(*), sum(amount) FROM transactions GROUP BY 1 ORDER BY 1;"
```

In the Catalyst console (<https://catalyst.diagrid.io>), the `customer-7` workflow appears under the **Workflows** tab for `agent-worker`, with each activity invocation visible in the trace.

## 6. Tear down

```bash
diagrid dev stop -f dapr.yaml
docker compose -f local/compose.yaml down -v
```

To delete the Catalyst project entirely:

```bash
diagrid project delete my-project
```

> Re-provisioning is the only way to add `--enable-agent-infrastructure` to a project — there's no `project update` for it.

## Notes & gotchas

- **The stub agent's tool-call loop is inherently bounded** — the LangGraph state machine in `services/agent-langgraph/agent_worker/stub_llm.py` alternates `get_next_task` / `get_balance` / `credit_account` / `report_done`, one credit per workflow instance, then stops. `DaprWorkflowGraphRunner`'s own `max_steps` (default 100, passed as a `build_runner()` kwarg if you ever need to override it) is well above this and shouldn't need tuning.
- **Real LLM mode** (`STUB_LLM=false`, with `OPENAI_API_KEY` set in `dapr.yaml`'s `env:`) only works with a model that emits OpenAI-compatible structured tool calls.
- **Tool calls fail with `403 Forbidden`** — no MCP access grant yet for `agent-worker`, or it's missing one of the four tools. Re-run the `diagrid mcpserver access grant` command in step 2.
- **Workflow name** is the most common stumbling block — see [TROUBLESHOOTING.md](./TROUBLESHOOTING.md#orchestratornotregistererror-a-x-orchestrator-was-not-registered).
- **Postgres data persists** across `docker compose down` unless `-v` is passed. The `transactions` table accumulates across runs — clear with `TRUNCATE transactions, execution_runs RESTART IDENTITY CASCADE` if you want a clean count, or just hit Reset in the UI which starts a fresh `execution_run`.
