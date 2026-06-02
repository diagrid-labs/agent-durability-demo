# Note for the dapr-agents / Catalyst engineering teams

Workflow scheduling name behaves inconsistently across three runtime targets that all use the same `dapr-agents == 1.0.1` library and the same agent code. Filing this so the teams can decide where the canonical fix lives.

## Three environments, three behaviors

Same `DurableAgent` profile (`AgentProfileConfig(name="banker", ...)`). Same agent process. Different deployment targets. Schedule via `DaprWorkflowClient.schedule_new_workflow(workflow=_wf_proxy, ...)`.

| Runtime target | Scheduled name that works | Scheduled name that fails |
|---|---|---|
| **Upstream Dapr** (`dapr init -k`, sidecar in each pod) | `dapr.agents.Banker.workflow` | `agent_workflow` → `OrchestratorNotRegisteredError` |
| **Catalyst Local** (`diagrid dev run`, local daprd talking to Catalyst) | `agent_workflow` | `dapr.agents.Banker.workflow` (per earlier test) |
| **Catalyst Cloud** (k8s pods, no sidecar, env-var-only) | `dapr.agents.Banker.workflow` | `agent_workflow` → `OrchestratorNotRegisteredError` |

The middle row is the odd one out. Local Catalyst accepts the short alias that neither Upstream Dapr nor Catalyst Cloud accept.

## What the SDK actually registers

On `DurableAgent.start(runtime=runtime, auto_register=True)`, the Python worker's `WorkflowRuntime` logs a single entry:

```
WorkflowRuntime INFO: Registering workflow 'dapr.agents.Banker.workflow' with runtime
```

So `dapr.agents.{TitleCase(ProfileName)}.workflow` is what the local Python worker has in its orchestrator registry. There's no second alias registered.

## Likely explanation (inference, not verified against code)

The Python worker's lookup is strict name matching. When the workflow engine dispatches a work item, it sends the scheduled name; the worker fails open if that exact string isn't in its registry.

- **Upstream Dapr** dispatches back to the worker with whatever name was scheduled. Strict match against `dapr.agents.Banker.workflow` is required.
- **Catalyst Cloud** with the SDK going straight to Catalyst over gRPC: same story — the dispatch comes back with the scheduled name, worker requires strict match.
- **Catalyst Local** has a local daprd in the path (`diagrid dev run` spawns one). Either daprd or `diagrid dev run` appears to insert an alias such that `agent_workflow` resolves to the actual registered name. This alias seems to *only* exist on the Local path.

The current sentinel we used to discriminate (`DAPR_HTTP_ENDPOINT` set) is set in *both* Local and Cloud — so it can't distinguish them — which is what put this workaround into a corner.

## What we ended up doing

Removed the env-based swap entirely. We now always schedule under the fully-qualified name the SDK registers:

```python
workflow_name = os.environ.get(
    "FORCE_WORKFLOW_NAME", "dapr.agents.Banker.workflow"
)
```

`FORCE_WORKFLOW_NAME` is the escape hatch — on the one runtime target this might break (Catalyst Local), the operator can set `FORCE_WORKFLOW_NAME=agent_workflow` to fall back to the short alias. We've left this knob in `services/agent/agent_worker/main.py`.

## Discussion points

1. **One canonical schedule name.** Pick a side and make every runtime accept it. Options:
   - SDK registers both names locally (qualified *and* short alias). Then upstream Dapr accepts the short alias too and Local Catalyst keeps working as-is.
   - Catalyst Local removes its alias layer so the short name doesn't work there either. Forces every caller to use the qualified name. Tightest consistency.
   - Catalyst Cloud and upstream Dapr learn to accept the short alias. Equalizes the other direction.

2. **Document the alias affordance.** If the Catalyst Local alias layer is intentional, it should be documented under "schedule a workflow" — currently there's no hint that the workflow name on the schedule side differs from what the SDK registers.

3. **`schedule_new_workflow` API ergonomics.** Today the SDK takes a function reference and reads its `__name__`. We end up creating a `_wf_proxy() pass` stub with a forced `__name__` because we can't get a handle on the actual orchestrator callable from outside the agent process. Add a `schedule_new_workflow(workflow_name: str, input=..., instance_id=...)` overload so this hack isn't needed.

4. **Surface registration mismatches at schedule time.** Today the schedule call returns success, the workflow appears in the engine, then immediately fails with `OrchestratorNotRegisteredError` from the worker. The engine should either:
   - Reject schedules whose name doesn't match any registered worker for the app-id ("workflow 'X' not registered for app-id 'agent-worker'; known: Y, Z").
   - Or surface this error back to the schedule caller synchronously instead of as a delayed failure visible only in the workflow execution log.

   The current behavior costs several hours of "the schedule succeeded but my workflow is failing — where do I look?" per developer per environment.

## Repro

`services/agent/agent_worker/main.py` in this repo has both the unified-name code (default) and the `FORCE_WORKFLOW_NAME` escape hatch wired in:

- **Catalyst Cloud on AKS**: deploy via `deploy/agent` chart with `catalyst.enabled=true` (see `CATALYST_CLOUD.md`). Default schedule name = qualified. Works.
- **Upstream Dapr on AKS**: deploy via `deploy/agent` chart with `dapr.enabled=true`. Default schedule name = qualified. Works.
- **Catalyst Local**: `diagrid dev run --file dapr.yaml` (see `CATALYST.md`). Default schedule name = qualified. *Untested under the unified-name change — if it breaks, set `FORCE_WORKFLOW_NAME=agent_workflow` to fall back to what we know worked previously.*

Happy to walk through the logs / repro on a call.
