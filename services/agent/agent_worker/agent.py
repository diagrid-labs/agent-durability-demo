"""LangGraph graph + DaprWorkflowGraphRunner setup for the Bank Creditor demo."""

import json
import os
from typing import Any

from diagrid.agent.langgraph import DaprWorkflowGraphRunner
from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langgraph.graph import START, MessagesState, StateGraph

from .mcp_client import call_tool
from .stub_llm import StubLLM


@tool
async def get_balance(customer_id: int) -> dict[str, Any]:
    """Look up a customer's current balance and target."""
    return await call_tool("get_balance", {"customer_id": customer_id})


@tool
async def credit_account(
    customer_id: int,
    amount: int,
    tx_id: str,
    agent_id: str,
    execution_run_id: int,
) -> dict[str, Any]:
    """Idempotently credit an amount to a customer's account within an
    execution run.

    The (execution_run_id, tx_id) pair is the idempotency key — duplicates
    are absorbed by the database and return applied=false. Always pass
    through the execution_run_id from get_next_task; never invent one."""
    return await call_tool(
        "credit_account",
        {
            "customer_id": customer_id,
            "amount": amount,
            "tx_id": tx_id,
            "agent_id": agent_id,
            "execution_run_id": execution_run_id,
        },
    )


@tool
async def get_next_task(requester: str) -> dict[str, Any]:
    """Ask the orchestrator for the next pending credit task. Pass your
    workflow identity (e.g. 'agent-007-task-3-r123') as `requester` so
    replays return the same task. Returns {done: true} when the queue is
    drained, otherwise {done: false, customer_id, tx_id, target, n}.

    Also forwards the pod hostname so the MCP server records slot→pod for
    the heatmap fleet view."""
    return await call_tool(
        "get_next_task",
        {"requester": requester, "pod": os.environ.get("HOSTNAME", "")},
    )


@tool
async def report_done(tx_id: str, applied: bool) -> dict[str, Any]:
    """Notify the orchestrator that the task identified by tx_id is complete."""
    return await call_tool("report_done", {"tx_id": tx_id, "applied": applied})


TOOLS = [get_balance, credit_account, get_next_task, report_done]
TOOLS_BY_NAME = {t.name: t for t in TOOLS}

SYSTEM_PROMPT = "\n".join(
    [
        "You are a banker agent. Credit one customer account by $1, then stop.",
        "Your prompt contains 'requester=<id>' (your stable workflow identity).",
        "Steps:",
        "1. get_next_task(requester=<your id>). If {done:true}, respond 'no work remaining'.",
        "2. get_balance(customer_id from the task).",
        "3. If balance >= target, report_done(tx_id, applied=false).",
        "4. Else credit_account(customer_id, amount=1, tx_id, agent_id='banker', execution_run_id), then report_done(tx_id, applied=true).",
        "5. Respond 'task complete' and stop.",
        "Always use the tx_id and execution_run_id from get_next_task. Never invent them.",
    ]
)


def _build_model() -> Any:
    if os.environ.get("STUB_LLM", "true").lower() != "false":
        return StubLLM().bind_tools(TOOLS)
    # Real-mode: OpenAI, matching the diagrid.agent.langgraph quickstart.
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(model=os.environ.get("AGENT_MODEL", "gpt-4o-mini")).bind_tools(
        TOOLS
    )


MODEL = _build_model()


async def _call_model_impl(state: MessagesState) -> dict:
    response = MODEL.invoke(state["messages"])
    return {"messages": [response]}


async def _call_tools_impl(state: MessagesState) -> dict:
    last_message = state["messages"][-1]
    results = []
    for tc in last_message.tool_calls:
        result = await TOOLS_BY_NAME[tc["name"]].ainvoke(tc["args"])
        results.append(
            ToolMessage(content=json.dumps(result), name=tc["name"], tool_call_id=tc["id"])
        )
    return {"messages": results}


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
def call_model(state: MessagesState):
    return _call_model_impl(state)


def call_tools(state: MessagesState):
    return _call_tools_impl(state)


def should_use_tools(state: MessagesState) -> str:
    last_message = state["messages"][-1]
    if getattr(last_message, "tool_calls", None):
        return "tools"
    return "__end__"


def build_graph():
    graph = StateGraph(MessagesState)
    graph.add_node("agent", call_model)
    graph.add_node("tools", call_tools)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", should_use_tools)
    graph.add_edge("tools", "agent")
    return graph.compile()


def build_runner() -> DaprWorkflowGraphRunner:
    return DaprWorkflowGraphRunner(
        graph=build_graph(),
        name="banker",
        role="Banker worker",
        goal="Process credit tasks from the orchestrator until the queue is empty",
    )
