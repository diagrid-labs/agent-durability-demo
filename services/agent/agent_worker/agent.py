"""DurableAgent setup for the Bank Creditor demo (dapr-agents 1.0.1)."""

import os
from typing import Any

from dapr_agents import DurableAgent, tool
from dapr_agents.agents.base import (
    AgentExecutionConfig,
    AgentProfileConfig,
    AgentStateConfig,
    StateStoreService,
)

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
    through the execution_run_id from GetNextTask; never invent one."""
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


class _SilentFormatter:
    """No-op replacement for dapr-agents' ColorTextFormatter.

    Under high concurrency, ColorTextFormatter prints colored output to
    stdout for every LLM turn. When the agent runs under `diagrid dev run`
    (which captures stdout), the pipe buffer fills faster than the parent
    drains it; once it breaks, every subsequent print raises BrokenPipeError
    and kills the activity. We silence it entirely — durabletask logs the
    important workflow events, MCP/orch logs cover the I/O."""

    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


def _build_llm():
    if os.environ.get("STUB_LLM", "true").lower() != "false":
        return StubLLM()
    # Real-mode: OpenAI. dapr-agents 1.0.1 has no built-in Anthropic client; if
    # we want Claude later we'd write a custom ChatClientBase subclass.
    from dapr_agents.llm.openai.chat import OpenAIChatClient

    return OpenAIChatClient(
        model=os.environ.get("AGENT_MODEL", "gpt-4o-mini"),
    )


def build_agent() -> DurableAgent:
    state_store = StateStoreService(
        store_name=os.environ.get("AGENT_STATE_STORE", "agent-memory"),
        key_prefix="banker-state",
    )
    agent = DurableAgent(
        # Required for Catalyst agent-infra (used to derive the pubsub topic).
        name="banker",
        profile=AgentProfileConfig(
            name="banker",
            role="Banker worker",
            goal="Process credit tasks from the orchestrator until the queue is empty",
            instructions=[
                "You are a banker agent. Credit one customer account by $1, then stop.",
                "Your prompt contains 'requester=<id>' (your stable workflow identity).",
                "Steps:",
                "1. GetNextTask(requester=<your id>). If {done:true}, respond 'no work remaining'.",
                "2. GetBalance(customer_id from the task).",
                "3. If balance >= target, ReportDone(tx_id, applied=false).",
                "4. Else CreditAccount(customer_id, amount=1, tx_id, agent_id='banker', execution_run_id), then ReportDone(tx_id, applied=true).",
                "5. Respond 'task complete' and stop.",
                "Always use the tx_id and execution_run_id from GetNextTask. Never invent them.",
            ],
        ),
        state=AgentStateConfig(store=state_store),
        execution=AgentExecutionConfig(max_iterations=10),
        tools=[get_balance, credit_account, get_next_task, report_done],
        llm=_build_llm(),
    )
    # Replace the colored stdout printer; under high concurrency its writes
    # break diagrid dev run's captured stdout pipe.
    agent.text_formatter = _SilentFormatter()
    return agent
