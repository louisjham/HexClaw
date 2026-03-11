import asyncio
import json
import logging
import os
from typing import Dict, Any, Optional
from mcp.client.stdio import stdio_client, StdioServerParameters
from mcp.client.session import ClientSession

# We assume Villager AI integration is in c:\antigravity\villager\villager-ai-hexstrike-integration
# adjust path if needed:
VILLAGER_SERVER_SCRIPT = os.getenv(
    "VILLAGER_MCP_SCRIPT", 
    "C:/antigravity/villager/villager-ai-hexstrike-integration/src/villager_ai/mcp/villager_proper_mcp.py"
)

log = logging.getLogger("hexclaw.villager_client")

async def dispatch_mission(mission: str, target_scope: str, constraints: list = None) -> Optional[str]:
    """
    Connect to Villager MCP Server, call `create_mission`, and return the task_id.
    """
    server_params = StdioServerParameters(
        command="python",
        args=[VILLAGER_SERVER_SCRIPT]
    )

    try:
        async with stdio_client(server_params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                log.info(f"Successfully initialized MCP connection to Villager server.")
                
                result = await session.call_tool(
                    "create_mission", 
                    arguments={
                        "mission": mission,
                        "target_scope": target_scope,
                        "constraints": constraints or []
                    }
                )
                
                # The MCP stdio server returns a list of TextContent
                text_result = result.content[0].text
                data = json.loads(text_result)
                
                if data.get("success"):
                    task_id = data.get("task_id")
                    log.info(f"Villager Mission Dispatched - Task ID: {task_id}")
                    return task_id
                else:
                    log.error(f"Failed to dispatch mission: {data}")
                    return None
    except Exception as e:
        log.error(f"Error communicating with Villager MCP: {e}")
        import traceback
        traceback.print_exc()
        return None

async def poll_task(task_id: str) -> Dict[str, Any]:
    """
    Connect to Villager MCP Server, poll `get_task_status`, and return the state.
    """
    server_params = StdioServerParameters(
        command="python",
        args=[VILLAGER_SERVER_SCRIPT]
    )

    try:
        async with stdio_client(server_params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                
                result = await session.call_tool(
                    "get_task_status", 
                    arguments={"task_id": task_id}
                )
                
                text_result = result.content[0].text
                return json.loads(text_result)
    except Exception as e:
        log.error(f"Error polling task {task_id}: {e}")
        return {"status": "error", "error": str(e)}

# For quick test
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    
    async def main():
        task_id = await dispatch_mission("Deep scan test", "127.0.0.1")
        if task_id:
            status = await poll_task(task_id)
            print(f"Polled status: {status}")
            
    asyncio.run(main())
