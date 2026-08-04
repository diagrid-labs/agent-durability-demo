"""Plain LangGraph graph for the Bank Creditor demo — no Dapr, no Catalyst.

Mirrors services/agent/agent_worker/agent.py's graph shape exactly (same
BankerState, same credit_next tool, same agent/tools loop) but with no
DaprWorkflowGraphRunner wrapping. Durability here is whatever plain LangGraph
gives you for free (nothing, once the process dies) — that absence is the
point of this deployment; see docs/LOCAL_DAPR.md and CLAUDE.md for the
comparison this exists to demonstrate.

Since there's no diagrid.agent.langgraph node registry involved, the
`async def` node gotcha documented in CLAUDE.md / the other agent.py doesn't
apply here — plain LangGraph nodes can just be `async def` directly.
"""

import os
from typing import Any, Optional, TypedDict

from langchain_core.tools import tool
from langgraph.graph import START, StateGraph

from .mcp_client import call_tool
from .stub_llm import StubLLM


class BankerState(TypedDict):
    requester: str
    customer_id: int
    last_result: Optional[dict]
    done: bool
    final_message: Optional[str]


@tool
async def credit_next(requester: str, customer_id: int) -> dict[str, Any]:
    """Claim, evaluate, and (if needed) apply this customer's next $1 credit
    — one MCP call per credit. `customer_id` is the account this instance is
    permanently bound to for the whole run; pass `requester` (your stable
    instance identity) every call so replays claim the same in-flight credit
    instead of popping a new one.

    Returns {done: true} once that customer's 100 credits are exhausted,
    otherwise {done: false, applied, tx_id, balance, n}."""
    return await call_tool(
        "credit_next",
        {
            "requester": requester,
            "customer_id": customer_id,
            "pod": os.environ.get("HOSTNAME", ""),
        },
    )


def _build_model() -> Any:
    if os.environ.get("STUB_LLM", "true").lower() != "false":
        return StubLLM()
    raise RuntimeError(
        "STUB_LLM=false is not supported: BankerState has no message "
        "transcript for a real chat model to reason over."
    )


MODEL = _build_model()


async def call_model(state: BankerState) -> dict:
    return MODEL.decide(state)


async def call_tools(state: BankerState) -> dict:
    result = await credit_next.ainvoke(
        {"requester": state["requester"], "customer_id": state["customer_id"]}
    )
    return {"last_result": result}


def should_use_tools(state: BankerState) -> str:
    return "__end__" if state.get("done") else "tools"


def build_graph():
    graph = StateGraph(BankerState)
    graph.add_node("agent", call_model)
    graph.add_node("tools", call_tools)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", should_use_tools)
    graph.add_edge("tools", "agent")
    return graph.compile()


GRAPH = build_graph()
