# Running locally on plain Dapr (no Catalyst)

**This is not a supported deployment path.** For actually running the demo, see
[DEPLOYMENT.md](./DEPLOYMENT.md) (Kubernetes) or [CATALYST.md](./CATALYST.md) (local +
Catalyst). This doc is a debugging/investigation recipe: a way to run the agent against a
plain local Dapr install — no Catalyst project, no MCP proxy, no gateway — so you can test
Dapr Workflow's own durability behavior in isolation from Catalyst's hosted backend.

Use this when you need to answer "is this a Catalyst-specific bug, or a general Dapr
Workflow limitation?" That's exactly what it was built for — see the "does NOT reproduce
under plain local Dapr" note on the pod-kill-mid-activity gotcha in `CLAUDE.md`.

## Why a patch is required

`services/agent/agent_worker/mcp_client.py` hardcodes Catalyst's MCP proxy path
(`$DAPR_HTTP_ENDPOINT/v1.0/diagrid/mcp/$MCP_SERVER_NAME`), which doesn't exist outside
Catalyst — there's no plain-Dapr fallback in the codebase (see `CLAUDE.md`'s gotcha on this;
it's an intentional simplification, not an oversight). To run locally without Catalyst, you
need to temporarily bypass that proxy and call the MCP server directly. Don't leave this
patch in place — it's for this investigation only.

Edit `mcp_client.py`:

```python
DAPR_HTTP_ENDPOINT = os.environ.get("DAPR_HTTP_ENDPOINT", "http://localhost:3500")
DAPR_API_TOKEN = os.environ.get("DAPR_API_TOKEN", "")
MCP_SERVER_NAME = os.environ.get("MCP_SERVER_NAME", "bank-postgres-mcp")
# TEMPORARY — local-plain-Dapr-only escape hatch, see docs/LOCAL_DAPR.md. Not meant to stick
# around; revert once you're done.
MCP_DIRECT_URL = os.environ.get("MCP_DIRECT_URL", "")
MCP_URL = MCP_DIRECT_URL or f"{DAPR_HTTP_ENDPOINT}/v1.0/diagrid/mcp/{MCP_SERVER_NAME}"


async def call_tool(tool: str, args: dict[str, Any]) -> Any:
    """..."""
    headers = {} if MCP_DIRECT_URL else {"dapr-api-token": DAPR_API_TOKEN}
    async with streamablehttp_client(url=MCP_URL, headers=headers) as (read, write, _):
        ...
```

## 1. Prerequisites

- Dapr CLI + a local self-hosted runtime install (`dapr init`; tested against runtime
  1.18.2 — `dapr --version` to check yours, `dapr uninstall && dapr init --runtime-version
  X.Y.Z` to change it)
- Docker (for Postgres + the MCP server)
- `uv` (for the agent's Python environment)

## 2. Bring up Postgres + the MCP server

```bash
docker compose -f local/compose.yaml up -d --build
curl -s http://localhost:9000/healthz   # {"status":"ok"}
```

## 3. Set up the agent's Python environment

```bash
cd services/agent
uv sync
```

## 4. Run two agent instances sharing one app-id

This is the key trick: two `dapr run` processes with the *same* `--app-id`, on different
ports, both registering with the same local placement/scheduler. That's what mimics two pod
replicas of the same Deployment — Dapr's actor placement can hand an orphaned activity from
one to the other if one process dies.

```bash
# Terminal / background job A
cd services/agent
STUB_LLM=true MCP_DIRECT_URL=http://localhost:9000/mcp/ HOSTNAME=local-a \
  dapr run --app-id bank-agent-creditor --app-port 8000 -H 3500 -G 50001 -M 9091 \
  -- uv run uvicorn agent_worker.main:app --host 0.0.0.0 --port 8000

# Terminal / background job B
cd services/agent
STUB_LLM=true MCP_DIRECT_URL=http://localhost:9000/mcp/ HOSTNAME=local-b \
  dapr run --app-id bank-agent-creditor --app-port 8001 -H 3501 -G 50002 -M 9092 \
  -- uv run uvicorn agent_worker.main:app --host 0.0.0.0 --port 8001
```

Check both came up clean: `curl http://localhost:8000/healthz` and `:8001/healthz`. The
daprd log for each should show `Reporting initial host to placement service with initial
types [... .workflow .activity]` — confirms it registered as a worker for this app-id.

## 5. Start a workflow directly (skip the MCP replenisher)

You don't need `/agent/spawn` or the replenisher for this — just hit one instance's
`/schedule-one` directly, same shape the replenisher would use:

```bash
curl -X POST http://localhost:8000/schedule-one -H 'Content-Type: application/json' \
  -d '{"instance_id": "test-local-1", "customer_id": 1}'
```

Optionally slow credits down so you have a real window to catch a "mid-activity" moment
(instead of it racing through 100 credits in a couple seconds on localhost):

```bash
curl -X POST http://localhost:9000/chaos/latency -H 'Content-Type: application/json' \
  -d '{"ms": 1500, "duration_ms": 60000}'
```

## 6. Kill one process mid-activity

Find the full process tree for the instance you want to kill (`dapr run`, `daprd`, `uv`,
`uvicorn` — all four, to actually simulate the pod dying rather than just the app):

```bash
ps aux | grep -E "daprd|uvicorn|dapr run" | grep -v grep
kill -9 <dapr-run-pid> <daprd-pid> <uv-pid> <uvicorn-pid>
```

## 7. Watch for recovery — and verify the survivor is actually alive

```bash
watch -n2 "curl -s http://localhost:9000/orch/customers | python3 -m json.tool"
```

**Important:** actively poll the surviving process's `/healthz` in the same loop. If the
balance stops climbing, confirm the survivor is still up before concluding anything froze —
if *both* processes are gone, that's not a Dapr behavior, it's an environment problem
(long-lived background shells can get silently reaped in some tool environments). Don't
count a "freeze" unless you've verified the survivor was healthy the whole time.

## 8. Clean up

```bash
kill -9 <any remaining dapr run / daprd / uv / uvicorn PIDs>
docker compose -f local/compose.yaml down
git checkout -- services/agent/agent_worker/mcp_client.py   # revert the patch above
```

## What this found (2026-07-31)

Across 3 verified trials — kills as early as 2-7 of 100 credits in, survivor's `/healthz`
actively polled throughout — the surviving process always picked up the orphaned activity
and the workflow ran to completion. No freeze, ever. This is a strong signal that the
pod-kill-mid-activity non-resume issue documented in `CLAUDE.md` is specific to Catalyst's
hosted backend, not a general Dapr Workflow limitation.

Caveat: local self-hosted Dapr's workflow engine runs on the classic actor-placement backend
(`worker started with backend dapr.actors/v1` in the daprd log, backed by Redis) — a
different implementation from whatever Catalyst's own hosted durabletask backend uses. So
this narrows the issue to "Catalyst's backend specifically," not proof the exact same code
path is at fault.

## Unrelated thing you'll probably hit

`mcp>=1.2.0` (both `services/agent/pyproject.toml` and `services/mcp/pyproject.toml`) had no
upper bound until `mcp` 2.0.0 shipped and relocated/removed `FastMCP`, breaking the build
(`ModuleNotFoundError: No module named 'mcp.server.fastmcp'`). Already fixed with a
`<2.0.0` pin — just noting it here in case you hit the same error on a machine with an
older checkout.
