"""Plain LangGraph graph for the Bank Creditor demo — no Dapr, no Catalyst.
Same shape as services/agent-langgraph's graph, but with nothing durable underneath."""

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
    """Claim and apply this customer's next $1 credit. `requester` is this
    instance's stable identity, so replays reclaim the same in-flight credit."""
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
