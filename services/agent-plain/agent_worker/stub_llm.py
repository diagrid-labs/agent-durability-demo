"""Deterministic decision function for the orchestrator-driven Bank Creditor demo.

State machine over a small, FIXED-SIZE state dict — not a growing chat
transcript. One instance is permanently bound to one customer_id and repeats
a single `credit_next` MCP call until that customer's 100 credits are
exhausted:

    last_result           ↦ next step
    -----------------------------
    (none)                → call credit_next
    {done: true}          → stop
    {done: false, ...}    → call credit_next again

The loop bound (100 credits) lives in the orchestrator's per-customer queue
length, not a counter here — the stub only ever reacts to the last result,
so it stays a pure function of state. Identical logic to the Dapr-backed
agent's stub_llm.py (services/agent/agent_worker/stub_llm.py) — this state
shape isn't accumulating-transcript-shaped for framework-agnostic reasons,
not because of any gRPC/Catalyst-specific constraint here.
"""

from typing import Any


class StubLLM:
    """Deterministic decision function: state in, partial state update out."""

    def decide(self, state: dict[str, Any]) -> dict[str, Any]:
        last_result = state.get("last_result")
        if last_result and last_result.get("done"):
            return {"done": True, "final_message": "account complete"}
        return {}
