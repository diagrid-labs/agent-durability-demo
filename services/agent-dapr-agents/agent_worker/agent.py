"""Dapr Agents (`dapr_agents`) DurableAgent setup for the Bank Creditor demo.
One agent per pod, shared by every customer it serves — a second agent
object in the same process crashes on duplicate activity registration."""

import asyncio
import os
from typing import Any

from dapr_agents import DurableAgent, tool
from dapr_agents.agents.configs import (
    AgentExecutionConfig,
    AgentMemoryConfig,
    AgentPubSubConfig,
    AgentStateConfig,
)
from dapr_agents.memory.daprstatestore import ConversationDaprStateMemory
from dapr_agents.storage.daprstores.stateservice import StateStoreService

from .mcp_client import call_tool
from .stub_llm import StubLLM

# Fixed name Catalyst's `--enable-agent-infrastructure` provisions. See CLAUDE.md.
AGENT_MEMORY_STORE = "agent-memory"

# Reuses an existing topic name to avoid Catalyst's topic-discovery race and
# quota; pub/sub itself is unused since everything runs through /schedule-one.
AGENT_PUBSUB_TOPIC = "bank-creditor-1.topic"
AGENT_BROADCAST_TOPIC = "agents.broadcast"

# One turn per credit (decide, then credit_next) — 100 credits needs ~100;
# sized with headroom like the LangGraph variant's max_steps=400.
MAX_ITERATIONS = 150

# Paces the demo so a run stays watchable on stage — without it 100 credits
# clear in a couple seconds, too fast to trigger and observe chaos mid-run.
CREDIT_PACE_SECONDS = 0.3


@tool
async def credit_next(requester: str, customer_id: int) -> dict[str, Any]:
    """Claim and apply this customer's next $1 credit. `requester` is this
    instance's stable identity, so replays reclaim the same in-flight credit."""
    result = await call_tool(
        "credit_next",
        {
            "requester": requester,
            "customer_id": customer_id,
            "pod": os.environ.get("HOSTNAME", ""),
        },
    )
    await asyncio.sleep(CREDIT_PACE_SECONDS)
    return result


def build_agent() -> DurableAgent:
    """One agent per pod; each `/schedule-one` starts a new workflow instance
    against it, one per customer."""
    if os.environ.get("STUB_LLM", "true").lower() == "false":
        # Not supported: the stub reads decision state from credit_next's own
        # result, not a real conversational transcript.
        raise RuntimeError("STUB_LLM=false is not supported for this demo agent.")

    return DurableAgent(
        name="bank-creditor",
        role="Bank Creditor",
        goal="Credit a customer's account to by $100, one dollar at a time.",
        tools=[credit_next],
        llm=StubLLM(),
        state=AgentStateConfig(store=StateStoreService(store_name=AGENT_MEMORY_STORE)),
        memory=AgentMemoryConfig(
            store=ConversationDaprStateMemory(store_name=AGENT_MEMORY_STORE)
        ),
        # No `registry=`: pods racing to register the same name can hang for
        # minutes in an ETag retry loop, and it's not functionally required.
        pubsub=AgentPubSubConfig(
            pubsub_name="agent-pubsub",
            agent_topic=AGENT_PUBSUB_TOPIC,
            broadcast_topic=AGENT_BROADCAST_TOPIC,
        ),
        execution=AgentExecutionConfig(max_iterations=MAX_ITERATIONS),
    )
