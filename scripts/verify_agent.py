"""Verify the web agent exposes only FreeCAD; never submits a model task."""
import asyncio
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def main():
    from agent.client import AgentClient
    from agent.runtime import WORKSPACE
    from backend.app.config import Settings
    from backend.app import freecad
    async with AgentClient(Settings(), directory=str(WORKSPACE)) as client:
        health = await client.health()
        mcp = await client.mcp_status()
        if not health.get('healthy') or set(mcp) != {'freecad'} or mcp['freecad'].get('status') != 'connected':
            raise SystemExit('配置不正确：必须连接唯一 FreeCAD MCP，不允许旧 CAD 工具回退。')
        print('网页智能体已连接原版 FreeCAD MCP；旧工具链未注册。')
        print('FreeCAD：' + ('就绪' if await freecad.idle() else '忙碌或不可用'))


if __name__ == '__main__':
    asyncio.run(main())
