import asyncio
import logging
import os
from mcp.client.stdio import stdio_client, StdioServerParameters
from mcp.client.session import ClientSession

logging.basicConfig(level=logging.DEBUG)

VILLAGER_SERVER_SCRIPT = "C:/antigravity/villager/villager-ai-hexstrike-integration/src/villager_ai/mcp/villager_proper_mcp.py"

async def test():
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    server_params = StdioServerParameters(
        command="python",
        args=[VILLAGER_SERVER_SCRIPT],
        env=env
    )
    async with stdio_client(server_params) as (read_stream, write_stream):
        print("Streams opened!")
        async with ClientSession(read_stream, write_stream) as session:
            print("Session ready, initializing...")
            await session.initialize()
            print("Init ok!")

asyncio.run(test())
