"""DurableAgent setup for the Bank Heist demo (dapr-agents 1.0.1)."""

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
    customer_id: int, amount: int, tx_id: str, agent_id: str
) -> dict[str, Any]:
    """Idempotently credit an amount to a customer's account.

    tx_id must be unique per logical credit step; duplicate tx_ids are
    absorbed by the database and return applied=false."""
    return await call_tool(
        "credit_account",
        {
            "customer_id": customer_id,
            "amount": amount,
            "tx_id": tx_id,
            "agent_id": agent_id,
        },
    )


@tool
async def get_next_task(requester: str) -> dict[str, Any]:
    """Ask the orchestrator for the next pending credit task. Pass your
    workflow identity (e.g. 'slot-7-task-3-r123') as `requester` so replays
    return the same task. Returns {done: true} when the queue is drained,
    otherwise {done: false, customer_id, tx_id, target, n}."""
    return await call_tool("get_next_task", {"requester": requester})


@tool
async def report_done(tx_id: str, applied: bool) -> dict[str, Any]:
    """Notify the orchestrator that the task identified by tx_id is complete."""
    return await call_tool("report_done", {"tx_id": tx_id, "applied": applied})


@tool
async def process_task(requester: str) -> dict[str, Any]:
    """Atomic single-call alternative to GetNextTask + GetBalance +
    CreditAccount + ReportDone. Pass your workflow identity as `requester`."""
    return await call_tool("process_task", {"requester": requester})


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
    # Catalyst's agent infrastructure auto-provisions `agent-memory` (state.diagrid).
    # In sidecar/k8s mode the Helm chart writes a Component named `agent-memory`
    # that points at the Postgres state store, so the same code path works there.
    state_store = StateStoreService(
        store_name=os.environ.get("AGENT_STATE_STORE", "agent-memory"),
        key_prefix="banker-state",
    )
    agent = DurableAgent(
        # Top-level name= is required when Catalyst agent infrastructure is
        # active: dapr-agents auto-discovers `agent-pubsub` and constructs an
        # agent topic from the local `name` param, not from profile.name.
        name="banker",
        profile=AgentProfileConfig(
            name="banker",
            role="Banker worker",
            goal="Process credit tasks from the orchestrator until the queue is empty",
            instructions=[
                "You process exactly ONE credit task and then stop.",
                "Your prompt contains 'requester=<id>' (your stable workflow "
                "identity) and 'mode=<multi|single>'.",
                "If mode=single: call ProcessTask with requester=<your id>. "
                "The result is either {done: true} (respond with 'no work "
                "remaining') or {done: false, tx_id, customer_id, applied}. "
                "Then respond with 'task complete' and stop.",
                "If mode=multi (default):",
                "  1. Call GetNextTask with requester=<your id>. The result "
                "is either {done: true} (respond 'no work remaining') or "
                "{done: false, customer_id, tx_id, target, n}.",
                "  2. Call GetBalance with the customer_id from the task.",
                "  3. If balance >= target, call ReportDone with "
                "applied=false.",
                "  4. Otherwise call CreditAccount with customer_id, "
                "amount=1, the task's tx_id, agent_id='banker', then call "
                "ReportDone with applied=true.",
                "  5. Respond with 'task complete' and stop.",
                "Never invent a tx_id — always use the one from GetNextTask "
                "or ProcessTask. Idempotency depends on it.",
            ],
        ),
        state=AgentStateConfig(store=state_store),
        # Single-task agents need only ~7 LLM iterations. 50 is generous.
        execution=AgentExecutionConfig(max_iterations=50),
        tools=[get_balance, credit_account, get_next_task, report_done, process_task],
        llm=_build_llm(),
    )
    # Replace the colored stdout printer; under high concurrency its writes
    # break diagrid dev run's captured stdout pipe.
    agent.text_formatter = _SilentFormatter()
    return agent
