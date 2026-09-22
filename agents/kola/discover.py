#!/usr/bin/env python3
"""List what the Kolaborate MCP server actually offers, so the allowlist is
chosen from the real tool list rather than from its marketing page.

    agents/.venv/Scripts/python agents/kola/discover.py            # names only
    agents/.venv/Scripts/python agents/kola/discover.py --describe # + descriptions

Prints tool names, their readOnlyHint, and how many there are. It never prints
the key, and it makes no call other than `tools/list`.

This is a one-off discovery aid, not part of the agent. The agent's allowlist is
a constant in main.py, because an allowlist read at runtime from the thing it is
supposed to be constraining is not an allowlist.
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))


def env_value(name: str) -> str:
    envfile = HERE / ".env"
    if not envfile.exists():
        sys.exit(f"{envfile} not found")
    for line in envfile.read_text(encoding="utf-8", errors="replace").splitlines():
        match = re.match(rf"\s*{re.escape(name)}\s*=(.*)$", line)
        if match:
            return match.group(1).strip().strip('"').strip("'")
    return ""


async def main() -> int:
    key = env_value("KOLA_API_KEY")
    url = env_value("KOLA_MCP_URL") or "https://mcp.kolaborate.africa/api/mcp"
    if not key:
        print("KOLA_API_KEY is empty in agents/kola/.env.")
        print("Paste the kola_live_... key after the = on that line, then rerun.")
        return 1
    print(f"key: {len(key)} chars, prefix {key.split('_')[0]}_ | endpoint: {url}\n")

    from mcp import ClientSession
    from mcp.client.streamable_http import (
        create_mcp_http_client, streamable_http_client)

    async with create_mcp_http_client(
            headers={"Authorization": f"Bearer {key}"}, timeout=120) as http:
        async with streamable_http_client(url, http_client=http) as streams:
            async with ClientSession(streams[0], streams[1],
                                     read_timeout_seconds=120) as session:
                await session.initialize()
                result = await session.list_tools()

    tools = sorted(result.tools or [], key=lambda t: t.name)
    print(f"{len(tools)} tools\n")
    read_only = 0
    for tool in tools:
        annotations = getattr(tool, "annotations", None)
        hint = getattr(annotations, "readOnlyHint", None) if annotations else None
        read_only += 1 if hint is True else 0
        line = f"  {'RO' if hint is True else '  '}  {tool.name}"
        if "--describe" in sys.argv:
            line += f"\n        {(tool.description or '')[:160]}"
        print(line)
    print(f"\n{read_only} annotated readOnlyHint=true, "
          f"{len(tools) - read_only} not annotated (treated as write-capable)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
