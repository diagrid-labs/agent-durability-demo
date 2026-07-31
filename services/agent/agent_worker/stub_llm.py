"""Deterministic decision function for the orchestrator-driven Bank Creditor demo.

State machine over a small, FIXED-SIZE state dict — not a growing chat
transcript. One workflow instance is permanently bound to one customer_id
and repeats a single `credit_next` MCP call until that customer's 100
credits are exhausted:

    last_result           ↦ next step
    -----------------------------
    (none)                → call credit_next
    {done: true}          → stop
    {done: false, ...}    → call credit_next again

The loop bound (100 credits) lives in the orchestrator's per-customer queue
length, not a counter here — the stub only ever reacts to the last result,
so it stays a pure function of state.

Why not LangGraph's MessagesState: that accumulates every AIMessage/
ToolMessage forever, and DaprWorkflowGraphRunner retransmits the FULL state
over gRPC on every activity call. At ~100 credits (~800 node steps under the
old 4-MCP-call-per-credit design) that transcript exceeded Catalyst's 4MB
gRPC message ceiling for every instance, independent of any chaos — confirmed
via `diagrid workflow get`, which showed `status: stalled` with
*"Workflow payload size ... exceeds 95% of max gRPC body size 4194304
bytes"*. A state dict with a handful of scalar/small-dict fields that get
*overwritten* each step instead of appended stays constant-size regardless of
credit count.
"""

from typing import Any


class StubLLM:
    """Deterministic decision function: state in, partial state update out."""

    def decide(self, state: dict[str, Any]) -> dict[str, Any]:
        last_result = state.get("last_result")
        if last_result and last_result.get("done"):
            return {"done": True, "final_message": "account complete"}
        return {}
