import json
import os
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

MCP_URL = os.environ.get("MCP_URL", "http://mcp.bank-heist.svc.cluster.local:8000/mcp")


async def call_tool(tool: str, args: dict[str, Any]) -> Any:
    # Per-call session: fine for the demo's call rate (~100 tx/sec peak).
    # Optimize to a pooled session in step 8 if profiling shows it matters.
    async with streamablehttp_client(MCP_URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool, args)
            for content in result.content:
                text = getattr(content, "text", None)
                if text is not None:
                    return json.loads(text)
            raise RuntimeError(f"no text content returned from {tool}")
