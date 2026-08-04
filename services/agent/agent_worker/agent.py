"""LangGraph graph + DaprWorkflowGraphRunner setup for the Bank Creditor demo."""

import os
from typing import Any, Optional, TypedDict

from diagrid.agent.langgraph import DaprWorkflowGraphRunner
from langchain_core.tools import tool
from langgraph.graph import START, StateGraph

from .mcp_client import call_tool
from .stub_llm import StubLLM


class BankerState(TypedDict):
    """Fixed-size state, overwritten each step — a growing MessagesState
    would blow Catalyst's 4MB gRPC payload ceiling (see stub_llm.py)."""

    requester: str
    customer_id: int
    last_result: Optional[dict]
    done: bool
    final_message: Optional[str]


@tool
async def credit_next(requester: str, customer_id: int) -> dict[str, Any]:
    """Claim and apply this customer's next $1 credit in one MCP call.
    `requester` is this workflow's stable identity, so replays reclaim the
    same in-flight credit. Returns {done: true} once 100 credits are used,
    else {done: false, applied, tx_id, balance, n}."""
    return await call_tool(
        "credit_next",
        {
            "requester": requester,
            "customer_id": customer_id,
            "pod": os.environ.get("HOSTNAME", ""),
        },
    )


# Metadata-only, for Catalyst's LangGraphMapper — call_tools invokes
# credit_next directly. The mapper's fallback tool scan requires
# callable(candidate), but a StructuredTool instance isn't callable, so the
# raw tool object gets silently rejected; a plain function proxy passes.
def _credit_next_metadata_proxy(*_args, **_kwargs):
    raise NotImplementedError("metadata-only stand-in for Catalyst's Agent UI — never invoked")


_credit_next_metadata_proxy.name = credit_next.name
_credit_next_metadata_proxy.description = credit_next.description
_credit_next_metadata_proxy.args = credit_next.args

TOOLS = [_credit_next_metadata_proxy]


def _build_model() -> Any:
    if os.environ.get("STUB_LLM", "true").lower() != "false":
        return StubLLM()
    # Real mode needs a message transcript to reason over; BankerState has none.
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


# diagrid.agent.langgraph only registers a node's *sync* callable (`.func`);
# `async def` nodes have `.func is None` and silently fail to register. These
# plain-def wrappers return the coroutine unawaited — the Dapr executor
# awaits it via its `asyncio.iscoroutine(result)` branch.
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
    # 100 credits × 2 steps (decide, credit_next) = 200; 400 gives headroom.
    return DaprWorkflowGraphRunner(
        graph=build_graph(),
        name="banker",
        max_steps=400,
        role="Banker worker",
        goal="Process one customer's credit tasks until none remain",
    )
