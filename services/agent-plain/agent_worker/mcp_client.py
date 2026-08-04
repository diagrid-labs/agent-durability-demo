import json
import os
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

# No Catalyst here — calls the MCP server's Service directly, same client
# as the Dapr-backed agent minus the proxy prefix/token.
MCP_URL = os.environ.get("MCP_URL", "http://mcp:80/mcp/")


async def call_tool(tool: str, args: dict[str, Any]) -> Any:
    """Call a tool on the MCP server. Opens a fresh session per call —
    simpler than sharing one across concurrent credit loops."""
    async with streamablehttp_client(url=MCP_URL) as (read, write, _):
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
