"""Deterministic decision function, duck-typed as `DurableAgent`'s `llm=`.
Reads customer_id and the last credit_next result out of message history."""

import json
import uuid
from typing import Any, Optional, Type

from pydantic import BaseModel

from dapr_agents.types.message import (
    AssistantMessage,
    FunctionCall,
    LLMChatCandidate,
    LLMChatResponse,
    ToolCall,
)


def _first_customer_id(messages: list[dict[str, Any]]) -> int:
    for message in messages or []:
        if message.get("role") == "user":
            content = message.get("content")
            if content:
                try:
                    return int(json.loads(content)["customer_id"])
                except (TypeError, ValueError, KeyError, json.JSONDecodeError):
                    continue
    raise ValueError("no role=user message with a customer_id payload found")


def _last_tool_result(messages: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    for message in reversed(messages or []):
        if message.get("role") == "tool":
            content = message.get("content")
            if content:
                try:
                    return json.loads(content)
                except (TypeError, json.JSONDecodeError):
                    return None
            return None
    return None


class StubLLM:
    """Deterministic decision function: messages in, an assistant message out."""

    def generate(
        self,
        messages: Optional[list[dict[str, Any]]] = None,
        *,
        response_format: Optional[Type[BaseModel]] = None,
        **_: Any,
    ) -> Any:
        messages = messages or []
        if response_format is not None:
            # `summarize`'s request — no customer_id payload, not part of
            # the decide loop. A fixed summary is fine; nothing reads it back.
            return response_format(summary="Credited this account via credit_next until done.")
        last_result = _last_tool_result(messages)
        if last_result and last_result.get("done"):
            message = AssistantMessage(content="account complete")
        else:
            customer_id = _first_customer_id(messages)
            requester = f"agent-dapr-{customer_id:03d}"
            arguments = {"requester": requester, "customer_id": customer_id}
            message = AssistantMessage(
                content=None,
                tool_calls=[
                    ToolCall(
                        id=uuid.uuid4().hex,
                        type="function",
                        function=FunctionCall(name="credit_next", arguments=arguments),
                    )
                ],
            )
        return LLMChatResponse(results=[LLMChatCandidate(message=message)])
