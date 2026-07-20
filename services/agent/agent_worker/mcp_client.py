import json
import os
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

DAPR_HTTP_ENDPOINT = os.environ.get("DAPR_HTTP_ENDPOINT", "http://localhost:3500")
DAPR_API_TOKEN = os.environ.get("DAPR_API_TOKEN", "")
MCP_SERVER_NAME = os.environ.get("MCP_SERVER_NAME", "bank-postgres-mcp")
MCP_URL = f"{DAPR_HTTP_ENDPOINT}/v1.0/diagrid/mcp/{MCP_SERVER_NAME}"


async def call_tool(tool: str, args: dict[str, Any]) -> Any:
    """Call a tool on the Postgres MCP server through Catalyst's managed MCP
    proxy. Opens a fresh session per call — simpler and safer than sharing
    one ClientSession across the many concurrent workflow activities this
    demo runs, at the cost of an extra initialize() round trip per call."""
    headers = {"dapr-api-token": DAPR_API_TOKEN}
    async with streamablehttp_client(url=MCP_URL, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool, args)
    if result.isError:
        raise RuntimeError(f"MCP tool error: {tool} -> {result.content}")
    for block in result.content:
        text = getattr(block, "text", None)
        if text is not None:
            return json.loads(text)
    raise RuntimeError(f"no text content returned from {tool}")
