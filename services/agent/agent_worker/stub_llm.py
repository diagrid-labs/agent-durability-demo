"""Deterministic LLM stub for the orchestrator-driven Bank Creditor demo.

State machine, no instance state — entirely a function of the chat history.
Per-task flow:

    last tool ↦ next emission
    -----------------------------
    (none)          → get_next_task
    get_next_task   → if done: stop; else get_balance(customer_id from task)
    get_balance     → if balance >= target: report_done(applied=false)
                      else credit_account(task.customer_id, 1, task.tx_id, "banker")
    credit_account  → report_done(applied=true)
    report_done     → stop

Replay determinism: DaprWorkflowGraphRunner executes each graph node as a
Dapr Workflow activity, so a node's output is loaded from history on replay
rather than re-derived. This stub is pure-functional too, so even direct
re-invocation is safe.
"""

import json
import re
import uuid
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage


class StubLLM:
    """Minimal stand-in for a LangChain chat model.

    Only implements the two methods the graph's `agent` node actually calls:
    `bind_tools` (to mirror `ChatOpenAI(...).bind_tools(tools)`) and `invoke`.
    """

    def bind_tools(self, tools: list[Any]) -> "StubLLM":
        return self

    def invoke(self, messages: list[BaseMessage]) -> AIMessage:
        last_tool_name, last_tool_content = self._last_tool_message(messages)
        requester = self._requester_from_messages(messages)

        if last_tool_name is None:
            return self._tool_call("get_next_task", {"requester": requester})

        if last_tool_name == "report_done":
            return self._content("task complete")

        if last_tool_name == "get_next_task":
            task = self._parse_json(last_tool_content) or {}
            if task.get("done"):
                return self._content("no work remaining")
            return self._tool_call(
                "get_balance", {"customer_id": int(task["customer_id"])}
            )

        if last_tool_name == "get_balance":
            task = self._find_active_task(messages)
            if task is None:
                # get_next_task was windowed out of history. Restart the
                # cycle — the orchestrator will hand us the same task again
                # (or a new one if the previous credit landed before a retry).
                return self._tool_call("get_next_task", {"requester": requester})
            balance = self._parse_balance(last_tool_content)
            if balance >= int(task["target"]):
                return self._tool_call(
                    "report_done", {"tx_id": str(task["tx_id"]), "applied": False}
                )
            return self._tool_call(
                "credit_account",
                {
                    "customer_id": int(task["customer_id"]),
                    "amount": 1,
                    "tx_id": str(task["tx_id"]),
                    "agent_id": "banker",
                    "execution_run_id": int(task["execution_run_id"]),
                },
            )

        if last_tool_name == "credit_account":
            task = self._find_active_task(messages)
            tx_id = str(task["tx_id"]) if task else self._tx_id_from_credit(messages)
            return self._tool_call("report_done", {"tx_id": tx_id, "applied": True})

        return self._content("done")

    # --- helpers ------------------------------------------------------------

    @staticmethod
    def _requester_from_messages(messages: list[BaseMessage]) -> str:
        """Pull `requester=...` out of the first human message in history.

        The orchestrator uses this for replay-idempotent task assignment, so
        it must be deterministic across replays — the prompt is part of the
        workflow input and persists in history."""
        for msg in messages:
            if not isinstance(msg, HumanMessage):
                continue
            content = msg.content
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        content = block.get("text", "")
                        break
            if not isinstance(content, str):
                content = str(content)
            m = re.search(r"requester=([^\s,;:]+)", content)
            if m:
                return m.group(1)
        return ""

    @staticmethod
    def _last_tool_message(messages: list[BaseMessage]) -> tuple[str | None, Any]:
        for msg in reversed(messages):
            if isinstance(msg, ToolMessage):
                return msg.name, msg.content
        return None, None

    @classmethod
    def _find_active_task(
        cls, messages: list[BaseMessage]
    ) -> dict[str, Any] | None:
        """Walk back through history for the most recent get_next_task result.

        Returns the parsed task dict, or None if it's been windowed out."""
        for msg in reversed(messages):
            if isinstance(msg, ToolMessage) and msg.name == "get_next_task":
                parsed = cls._parse_json(msg.content)
                if parsed and not parsed.get("done"):
                    return parsed
                return None
        return None

    @staticmethod
    def _tx_id_from_credit(messages: list[BaseMessage]) -> str:
        """Last-resort: pull tx_id from the assistant's credit_account tool call."""
        for msg in reversed(messages):
            if not isinstance(msg, AIMessage):
                continue
            for call in msg.tool_calls or []:
                if call.get("name") == "credit_account":
                    args = call.get("args") or {}
                    if "tx_id" in args:
                        return str(args["tx_id"])
        return ""

    @staticmethod
    def _parse_json(content: Any) -> dict[str, Any] | None:
        if content is None:
            return None
        if isinstance(content, dict):
            return content
        if isinstance(content, str):
            try:
                return json.loads(content)
            except (json.JSONDecodeError, TypeError):
                return None
        return None

    @classmethod
    def _parse_balance(cls, content: Any) -> float:
        parsed = cls._parse_json(content)
        if parsed and "balance" in parsed:
            try:
                return float(parsed["balance"])
            except (TypeError, ValueError):
                return 0.0
        return 0.0

    @staticmethod
    def _tool_call(name: str, arguments: dict[str, Any]) -> AIMessage:
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "id": f"call_{uuid.uuid4().hex[:12]}",
                    "name": name,
                    "args": arguments,
                }
            ],
        )

    @staticmethod
    def _content(text: str) -> AIMessage:
        return AIMessage(content=text)
