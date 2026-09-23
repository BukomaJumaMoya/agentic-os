#!/usr/bin/env python3
"""Boot all six agent MCP servers and check what they actually publish.

WHY THIS EXISTS SEPARATELY FROM THE STARTUP GUARD
-------------------------------------------------
`check_telegram_surface.py` reads Hermes' resolved surface. That is the right
check for "can a tool reach Telegram", but it answers from Hermes' side, and
Hermes caches MCP schemas. This script goes the other way: it spawns each agent
exactly as Hermes spawns it, over stdio, and asks the process itself.

Three things are compared against `hermes/surface-manifest.json`:

  1. the tool NAMES the server publishes, in both directions -- a tool missing
     from the manifest, and a manifest entry the server no longer provides
  2. the readOnlyHint ANNOTATION on each one. This is the whole approval gate:
     on an `untrusted` server, a tool whose hint is not exactly True must be
     approved by a human. So every read tool must carry it and every write tool
     must not, and a write tool that gained the annotation would silently stop
     asking. That asymmetry is why this is checked from the agent's own mouth.
  3. that the server starts at all with only its own `.env` present

Where a read tool costs nothing, it is also CALLED, so the result is "it
answered", not "it advertised a tool". Where a read tool would spend model
quota it is listed as schema-only rather than being called and reported as if
it were free -- the point of this script is to not overstate what was checked.

    agents/.venv/Scripts/python hermes/verify_agents.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENTS = ROOT / "agents"
PYTHON = AGENTS / ".venv" / "Scripts" / "python.exe"
MANIFEST = ROOT / "hermes" / "surface-manifest.json"

# server name -> (agent directory, a read tool that is free to call, its args)
# None means "every read tool this agent has costs model quota" -- see module
# docstring. draft_proposal and research are LLM calls; pm_query is an LLM call
# over ClickUp.
AGENTS_UNDER_TEST: dict[str, tuple[str, str | None, dict]] = {
    "research": ("research", None, {}),
    "pm": ("pm", None, {}),
    "coding_agent": ("coding", "get_status", {"job_id": "no-such-job"}),
    "docs_agent": ("docs", None, {}),
    "n8n_agent": ("n8n", "list_workflows", {}),
    "kola_agent": ("kola", "kola_catalogue", {}),
}


def _manifest() -> tuple[dict[str, str], set[str]]:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return data["tools"], set(data["write_tools"])


async def _probe(server: str, directory: str, free_tool: str | None,
                 free_args: dict) -> tuple[bool, list[str]]:
    """Spawn one agent, list its tools, optionally call one. Returns (ok, notes)."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    owned, write_tools = _manifest()
    expected = {name for name, owner in owned.items() if owner == server}

    params = StdioServerParameters(
        command=str(PYTHON),
        args=[str(AGENTS / directory / "main.py")],
        cwd=str(AGENTS / directory),
    )

    notes: list[str] = []
    ok = True
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()

            published = {t.name for t in listed.tools}
            if published != expected:
                ok = False
                for extra in sorted(published - expected):
                    notes.append(f"publishes {extra!r}, which is not in the manifest")
                for missing in sorted(expected - published):
                    notes.append(f"manifest expects {missing!r}, not published")

            for tool in listed.tools:
                ann = getattr(tool, "annotations", None)
                # The SDK's attribute is read_only_hint; readOnlyHint is only
                # its alias. Reading the alias off the object is exactly the
                # bug patch_readonly_hint.py exists to fix in Hermes -- so read
                # the attribute here, and fall back to the alias for a server
                # that returned a plain dict.
                hint = getattr(ann, "read_only_hint", None) if ann else None
                if hint is None and isinstance(ann, dict):
                    hint = ann.get("readOnlyHint")
                is_write = tool.name in write_tools
                if is_write and hint is True:
                    ok = False
                    notes.append(
                        f"{tool.name} is a write tool but is annotated "
                        "readOnlyHint: True -- it would stop asking for approval")
                elif not is_write and hint is not True:
                    ok = False
                    notes.append(
                        f"{tool.name} is a read tool but its readOnlyHint is "
                        f"{hint!r} -- every call would demand approval")

            if free_tool:
                result = await session.call_tool(free_tool, free_args)
                body = "".join(getattr(c, "text", "") for c in result.content)
                notes.append(f"called {free_tool} -> {len(body)} chars")
            else:
                notes.append("schema only (every read tool here spends quota)")

    return ok, notes


async def _main() -> int:
    manifest_tools, _ = _manifest()
    print(f"manifest: {MANIFEST.relative_to(ROOT).as_posix()}  "
          f"({len(manifest_tools)} tools, {len(AGENTS_UNDER_TEST)} agents)\n")

    failures = 0
    for server, (directory, free_tool, free_args) in AGENTS_UNDER_TEST.items():
        try:
            ok, notes = await _probe(server, directory, free_tool, free_args)
        except Exception as exc:                       # noqa: BLE001 - reported
            ok, notes = False, [f"did not start: {type(exc).__name__}: {exc}"]
        print(f"  {'PASS' if ok else 'FAIL'}  {server}")
        for note in notes:
            print(f"          {note}")
        failures += not ok

    print()
    if failures:
        print(f"FAIL: {failures} of {len(AGENTS_UNDER_TEST)} agent(s) did not "
              "match the manifest")
        return 1
    print(f"PASS: all {len(AGENTS_UNDER_TEST)} agents start, publish exactly "
          "their manifest tools, and annotate them correctly")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
