"""Deterministic LLM stub for the orchestrator-driven Bank Creditor demo.

State machine, no instance state — entirely a function of the chat history.
Per-task flow:

    last tool ↦ next emission
    -----------------------------
    (none)        → GetNextTask
    GetNextTask   → if done: stop; else GetBalance(customer_id from task)
    GetBalance    → if balance >= target: ReportDone(applied=false)
                    else CreditAccount(task.customer_id, 1, task.tx_id, "banker")
    CreditAccount → ReportDone(applied=true)
    ReportDone    → GetNextTask

Replay determinism: dapr-agents wraps each LLM call as a workflow activity, so
the activity output is loaded from history on replay rather than re-derived.
This stub is pure-functional too, so even direct re-invocation is safe.
"""

import json
import logging
import re
import uuid
from typing import Any

from dapr_agents.llm.chat import ChatClientBase
from dapr_agents.types.message import (
    AssistantMessage,
    LLMChatCandidate,
    LLMChatResponse,
)

log = logging.getLogger("stub_llm")


class StubLLM(ChatClientBase):
    @classmethod
    def from_prompty(cls, prompty_source, timeout=1500):  # noqa: ARG003
        return cls()

    def generate(
        self,
        messages: Any = None,
        *,
        input_data: dict[str, Any] | None = None,
        model: str | None = None,
        tools: list[Any] | None = None,
        response_format: Any = None,
        structured_mode: str = "json",
        stream: bool = False,
        **kwargs: Any,
    ) -> LLMChatResponse:
        msg_list = self._normalize_messages(messages)

        if response_format is not None:
            return self._build_default_model(response_format)

        last_tool_name, last_tool_content = self._last_tool_message(msg_list)
        requester = self._requester_from_messages(msg_list)

        if last_tool_name is None:
            return self._tool_call("GetNextTask", {"requester": requester})

        if last_tool_name == "ReportDone":
            return self._content("task complete")

        if last_tool_name == "GetNextTask":
            task = self._parse_json(last_tool_content) or {}
            if task.get("done"):
                return self._content("no work remaining")
            return self._tool_call(
                "GetBalance", {"customer_id": int(task["customer_id"])}
            )

        if last_tool_name == "GetBalance":
            task = self._find_active_task(msg_list)
            if task is None:
                # GetNextTask was windowed out of history. Restart the cycle —
                # the orchestrator will hand us the same task again (or a new
                # one if the previous credit landed before a retry).
                return self._tool_call("GetNextTask", {})
            balance = self._parse_balance(last_tool_content)
            if balance >= int(task["target"]):
                return self._tool_call(
                    "ReportDone", {"tx_id": str(task["tx_id"]), "applied": False}
                )
            return self._tool_call(
                "CreditAccount",
                {
                    "customer_id": int(task["customer_id"]),
                    "amount": 1,
                    "tx_id": str(task["tx_id"]),
                    "agent_id": "banker",
                    "execution_run_id": int(task["execution_run_id"]),
                },
            )

        if last_tool_name == "CreditAccount":
            task = self._find_active_task(msg_list)
            tx_id = str(task["tx_id"]) if task else self._tx_id_from_credit(msg_list)
            return self._tool_call(
                "ReportDone", {"tx_id": tx_id, "applied": True}
            )

        return self._content("done")

    # --- helpers ------------------------------------------------------------

    @staticmethod
    def _normalize_messages(messages: Any) -> list[dict[str, Any]]:
        if messages is None:
            return []
        if isinstance(messages, str):
            return [{"role": "user", "content": messages}]
        if isinstance(messages, dict):
            return [messages]
        out = []
        for m in messages:
            if hasattr(m, "model_dump"):
                out.append(m.model_dump())
            elif isinstance(m, dict):
                out.append(m)
            else:
                out.append({"role": "user", "content": str(m)})
        return out

    @staticmethod
    def _requester_from_messages(messages: list[dict[str, Any]]) -> str:
        """Pull `requester=...` out of the first user message in history.

        The orchestrator uses this for replay-idempotent task assignment, so
        it must be deterministic across replays — the prompt is part of the
        workflow input and persists in history."""
        for msg in messages:
            if str(msg.get("role", "")).lower() != "user":
                continue
            content = msg.get("content", "")
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
    def _last_tool_message(messages: list[dict[str, Any]]) -> tuple[str | None, Any]:
        for msg in reversed(messages):
            if msg.get("role") == "tool":
                return msg.get("name"), msg.get("content")
        return None, None

    @classmethod
    def _find_active_task(
        cls, messages: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        """Walk back through history for the most recent GetNextTask result.

        Returns the parsed task dict, or None if it's been windowed out."""
        for msg in reversed(messages):
            if msg.get("role") == "tool" and msg.get("name") == "GetNextTask":
                parsed = cls._parse_json(msg.get("content"))
                if parsed and not parsed.get("done"):
                    return parsed
                return None
        return None

    @staticmethod
    def _tx_id_from_credit(messages: list[dict[str, Any]]) -> str:
        """Last-resort: pull tx_id from the assistant's CreditAccount tool call."""
        for msg in reversed(messages):
            if msg.get("role") != "assistant":
                continue
            for call in msg.get("tool_calls") or []:
                fn = call.get("function") or {}
                if fn.get("name") == "CreditAccount":
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                        if "tx_id" in args:
                            return str(args["tx_id"])
                    except (json.JSONDecodeError, TypeError):
                        pass
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
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    try:
                        return json.loads(block.get("text", ""))
                    except (json.JSONDecodeError, TypeError):
                        continue
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
    def _build_default_model(model_cls: Any) -> Any:
        try:
            fields = getattr(model_cls, "model_fields", {})
            kwargs: dict[str, Any] = {}
            for name, info in fields.items():
                ann = str(info.annotation)
                if "str" in ann:
                    kwargs[name] = "stub"
                elif "int" in ann:
                    kwargs[name] = 0
                elif "float" in ann:
                    kwargs[name] = 0.0
                elif "bool" in ann:
                    kwargs[name] = False
                else:
                    kwargs[name] = None
            return model_cls(**kwargs)
        except Exception:
            return {"summary": "stub"}

    @staticmethod
    def _tool_call(name: str, arguments: dict[str, Any]) -> LLMChatResponse:
        tool_call = {
            "id": f"call_{uuid.uuid4().hex[:12]}",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }
        message = AssistantMessage(content=None, tool_calls=[tool_call])
        return LLMChatResponse(
            results=[LLMChatCandidate(message=message, finish_reason="tool_calls")],
            metadata={"stub": True},
        )

    @staticmethod
    def _content(text: str) -> LLMChatResponse:
        message = AssistantMessage(content=text)
        return LLMChatResponse(
            results=[LLMChatCandidate(message=message, finish_reason="stop")],
            metadata={"stub": True},
        )
