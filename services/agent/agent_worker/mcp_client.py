import json
import os
from itertools import count
from typing import Any

import httpx

MCP_URL = os.environ.get("MCP_URL", "http://mcp.bank-heist.svc.cluster.local/mcp/")
USE_DAPR_INVOKE = os.environ.get("USE_DAPR_INVOKE", "false").lower() == "true"
DAPR_MCP_APP_ID = os.environ.get("DAPR_MCP_APP_ID", "bank-mcp-server")

# FastMCP rejects POSTs that don't accept event-stream even in stateless mode.
_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}
_id_seq = count(1)


def _parse_event_stream(body: str) -> dict[str, Any]:
    for line in body.splitlines():
        if line.startswith("data:"):
            return json.loads(line[len("data:"):].strip())
    raise RuntimeError(f"no JSON payload in MCP response: {body[:200]!r}")


async def _call_via_dapr(tool: str, args: dict[str, Any]) -> Any:
    from dapr.aio.clients import DaprClient

    async with DaprClient() as d:
        resp = await d.invoke_method(
            app_id=DAPR_MCP_APP_ID,
            method_name=tool,
            data=json.dumps(args),
            content_type="application/json",
            http_verb="POST",
        )
    body = resp.data
    if isinstance(body, (bytes, bytearray)):
        body = body.decode("utf-8")
    return json.loads(body) if body else None


async def _call_via_httpx(tool: str, args: dict[str, Any]) -> Any:
    payload = {
        "jsonrpc": "2.0",
        "id": next(_id_seq),
        "method": "tools/call",
        "params": {"name": tool, "arguments": args},
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(MCP_URL, json=payload, headers=_HEADERS)
        r.raise_for_status()
        ctype = r.headers.get("content-type", "")
        data = r.json() if "application/json" in ctype else _parse_event_stream(r.text)

    if "error" in data:
        raise RuntimeError(f"MCP tool error: {data['error']}")
    for item in data.get("result", {}).get("content", []):
        text = item.get("text") if isinstance(item, dict) else None
        if text is not None:
            return json.loads(text)
    raise RuntimeError(f"no text content returned from {tool}")


async def call_tool(tool: str, args: dict[str, Any]) -> Any:
    if USE_DAPR_INVOKE:
        return await _call_via_dapr(tool, args)
    return await _call_via_httpx(tool, args)
