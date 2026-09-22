"""Inspect/call the original MCP over stdio; this is a diagnostic client, not a CAD API."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]


async def run(args: argparse.Namespace) -> None:
    parameters = StdioServerParameters(command="bash", args=[str(ROOT / "freecad/mcp.sh")])
    async with stdio_client(parameters) as (reader, writer):
        async with ClientSession(reader, writer, read_timeout_seconds=2 * args.timeout + 60) as session:
            await session.initialize()
            if not args.tool and not args.code:
                tools = await session.list_tools()
                print(json.dumps({"tools": [tool.name for tool in tools.tools]}, indent=2))
                return
            arguments = json.loads(args.arguments)
            tool = args.tool
            if args.code:
                tool = "execute_code"
                arguments = {"code": args.code.read_text(), "include_screenshot": False, "timeout": args.timeout}
            result = await session.call_tool(tool, arguments)
            # Save image blocks separately; never dump base64 onto the terminal.
            summary = {"is_error": result.is_error, "content": []}
            for index, block in enumerate(result.content):
                if block.type == "text":
                    summary["content"].append({"type": "text", "text": block.text})
                elif block.type == "image":
                    import base64

                    path = (args.report.parent if args.report else ROOT / ".runtime/freecad") / f"probe-{index}.png"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(base64.b64decode(block.data))
                    summary["content"].append({"type": "image", "path": str(path)})
            rendered = json.dumps(summary, ensure_ascii=False, indent=2)
            if args.report:
                args.report.parent.mkdir(parents=True, exist_ok=True)
                args.report.write_text(rendered + "\n")
            print(rendered)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tool", help="Original upstream MCP tool name")
    parser.add_argument("--arguments", default="{}", help="JSON tool arguments")
    parser.add_argument("--code", type=Path, help="Python script to send to execute_code")
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--report", type=Path, help="Save the actual MCP response")
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
