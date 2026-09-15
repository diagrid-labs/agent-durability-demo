"""Deterministic decision function: reacts only to the last credit_next
result, keeping state fixed-size instead of an ever-growing chat transcript."""

from typing import Any


class StubLLM:
    """Deterministic decision function: state in, partial state update out."""

    def decide(self, state: dict[str, Any]) -> dict[str, Any]:
        last_result = state.get("last_result")
        if last_result and last_result.get("done"):
            return {"done": True, "final_message": "account complete"}
        return {}
