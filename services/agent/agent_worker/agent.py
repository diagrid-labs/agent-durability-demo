"""LangGraph graph + DaprWorkflowGraphRunner setup for the Bank Creditor demo."""

import os
from typing import Any, Optional, TypedDict

from diagrid.agent.langgraph import DaprWorkflowGraphRunner
from langchain_core.tools import tool
from langgraph.graph import START, StateGraph

from .mcp_client import call_tool
from .stub_llm import StubLLM


class BankerState(TypedDict):
    """Fixed-size state — every field is overwritten each step, none grow.

    See stub_llm.py's module docstring for why this matters: a MessagesState-
    style accumulating transcript blows past Catalyst's 4MB gRPC payload
    ceiling well before 100 credits."""

    requester: str
    customer_id: int
    last_result: Optional[dict]
    done: bool
    final_message: Optional[str]


@tool
async def credit_next(requester: str, customer_id: int) -> dict[str, Any]:
    """Claim, evaluate, and (if needed) apply this customer's next $1 credit
    — one MCP call per credit. `customer_id` is the account this workflow
    instance is permanently bound to for the whole run; pass `requester` (your
    stable workflow identity) every call so replays claim the same in-flight
    credit instead of popping a new one.

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
    # Real-mode (STUB_LLM=false) isn't supported under this fixed-size state
    # shape — a real chat model needs an actual conversation to reason over,
    # which is exactly the unbounded-growth pattern this design avoids.
    # Restoring MessagesState would bring back the 4MB gRPC ceiling.
    raise RuntimeError(
        "STUB_LLM=false is not supported: BankerState has no message "
        "transcript for a real chat model to reason over."
    )


MODEL = _build_model()


async def _call_model_impl(state: BankerState) -> dict:
    return MODEL.decide(state)


async def _call_tools_impl(state: BankerState) -> dict:
    result = await credit_next.ainvoke(
        {"requester": state["requester"], "customer_id": state["customer_id"]}
    )
    return {"last_result": result}



# diagrid.agent.langgraph's node registry (as of diagrid[langgraph]==0.4.2)
# only extracts a node's *sync* callable (LangGraph's internal
# RunnableCallable.func) to hand to the Dapr activity — an `async def` node
# has `.func is None` (the coroutine function lives at `.afunc` instead), so
# it's silently never registered and the workflow fails at the first node
# with "not found in registry". Registering plain `def` wrappers that return
# the (unawaited) coroutine keeps LangGraph's node-type detection on the sync
# path while still giving the Dapr activity a coroutine to await — its
# executor already has an `asyncio.iscoroutine(result)` branch for exactly
# this shape.
def call_model(state: BankerState):
    return _call_model_impl(state)


def call_tools(state: BankerState):
    return _call_tools_impl(state)


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


def build_runner() -> DaprWorkflowGraphRunner:
    # DaprWorkflowGraphRunner defaults max_steps=100 (its per-instance graph-step
    # cap) — each credit now costs one (agent, tools) node-pair = 2 graph steps
    # (decide, credit_next), so 100 credits × 2 steps = 200, plus headroom for
    # the final done-check and any chaos-driven retries.
    return DaprWorkflowGraphRunner(
        graph=build_graph(),
        name="banker",
        max_steps=400,
        role="Banker worker",
        goal="Process one customer's credit tasks until none remain",
    )
