# Bank Heist on Catalyst — Local Mode

Run the dapr-agents Bank Heist demo on your laptop with the agent's Dapr APIs proxied through **Diagrid Catalyst**. Workflow runtime, state store, and placement run in Catalyst; the agent process, MCP server, and bank Postgres run locally.

> For a k8s deploy that uses Catalyst Cloud (no local `diagrid dev run`), see [CATALYST_CLOUD.md](./CATALYST_CLOUD.md).
> For the same k8s deploy with upstream Dapr instead of Catalyst, see the [main README](./README.md#deploying-to-kubernetes).

## Architecture

```
┌────────────────────┐     diagrid dev tunnel    ┌─────────────────────────┐
│ Catalyst project   │ ◄───────────────────────► │ agent-worker (uvicorn)  │
│  · workflow engine │                           │  ./services/agent       │
│  · state store     │                           │  appPort 8000           │
│  · placement       │                           │  daprHTTPPort 3500      │
└────────────────────┘                           └────────────┬────────────┘
                                                              │ MCP / streamable-http
                                                              ▼
                                                 ┌─────────────────────────┐
                                                 │ mcp (docker)            │
                                                 │  localhost:9000/mcp/    │
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

dapr-agents needs both managed workflow **and** agent infrastructure (managed conversation API, agent state components). Both must be set at project creation — they cannot be enabled on an existing project.

```bash
diagrid login
diagrid project create bank-heist-local \
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
diagrid project get bank-heist-local   # ManagedWorkflowStore: enabled (+ agent infra)
diagrid component list                 # agent-memory state.diagrid all app identities ready
```

When invoking the agent in step 4, set `AGENT_STATE_STORE=agent-memory` so the SDK looks up the right component.

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

## 3. Install agent dependencies

The agent runs as a normal Python process (not in docker) so `diagrid dev run` can attach the daprd tunnel to it.

```bash
cd services/agent
uv sync
cd -
```

## 4. Run the agent through Catalyst

From the repo root:

```bash
diagrid dev run --file dapr.yaml --project bank-heist-local \
  --skip-managed-kv --skip-managed-pubsub --skip-default-resiliency
```

The `--skip-*` flags prevent `dev run` from auto-creating duplicate default components on top of the ones agent-infrastructure already provisioned in §1.

Local Catalyst historically requires the short alias `agent_workflow` for the schedule name, while every other runtime target uses the fully-qualified `dapr.agents.Banker.workflow`. The agent code defaults to the qualified name; set the env override here:

```bash
# In dapr.yaml's `env:` section, or via shell before `diagrid dev run`:
export FORCE_WORKFLOW_NAME=agent_workflow
```

If you forget this, scheduled workflows will fail with `OrchestratorNotRegisteredError`. See [NOTES_FOR_DAPR_AGENTS.md](./NOTES_FOR_DAPR_AGENTS.md) for the engineering follow-up on this discrepancy.

This will:
1. Authenticate against your `bank-heist` project.
2. Open a dev tunnel from Catalyst back to `localhost:8000`.
3. Spawn `uvicorn agent_worker.main:app` and a local daprd that proxies all Dapr API calls to Catalyst.

You should see `agent + runtime started (stub=True)` once the FastAPI app finishes its `/v1.0/healthz/outbound` wait.

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
diagrid project delete bank-heist-local
```

> Re-provisioning is the only way to add `--enable-agent-infrastructure` to a project — there's no `project update` for it.

## Notes & gotchas

- **`max_iterations=300`** in `services/agent/agent_worker/agent.py` caps the stub agent at ~150 credits per workflow (it alternates `get_balance` / `credit_account` in multi-mode). The default single-mode `process_task` path only takes one iteration per task so this rarely matters now.
- **Real LLM mode** (`STUB_LLM=false`, with `OPENAI_API_KEY` set in `dapr.yaml`'s `env:`) only works with a model that emits OpenAI-compatible structured tool calls.
- **`MCP_URL` must end in a trailing slash** (`/mcp/`). FastMCP redirects `/mcp` → `/mcp/`, and the streamable-http client doesn't follow POST redirects.
- **Workflow name** is the most common stumbling block — see [TROUBLESHOOTING.md](./TROUBLESHOOTING.md#orchestratornotregistererror-a-x-orchestrator-was-not-registered).
- **Postgres data persists** across `docker compose down` unless `-v` is passed. The `transactions` table accumulates across runs — clear with `TRUNCATE transactions, execution_runs RESTART IDENTITY CASCADE` if you want a clean count, or just hit Reset in the UI which starts a fresh `execution_run`.
