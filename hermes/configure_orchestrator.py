#!/usr/bin/env python3
"""Configure Hermes as the orchestrator: three MCP agents, and nothing else.

Kept in the repository for the same reason as apply_approval_patch.py: Hermes
rewrites its own config.yaml (version migrations, `hermes tools`, onboarding),
so a hand edit is a change that survives until something re-serialises the
file. This script is the record of what the configuration is supposed to be,
and it is idempotent.

WHAT IT DOES
------------
1. Registers agents/research, agents/pm and agents/coding as stdio MCP servers,
   each with tools.include naming exactly its own tools, and resources and
   prompts off. An include list is a whitelist: a tool the agent grows later
   does not silently become available to Hermes.

2. Rewrites the Telegram toolset list to the three MCP servers plus memory,
   session_search and clarify. terminal, code_execution, file, computer_use and
   browser are removed, which is the point: the orchestrator routes, and the
   things that can run a command or write a file live behind an agent boundary
   with its own confinement.

3. Leaves a backup beside the file before touching it.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
It does not change the `cli` platform. Removing terminal from the CLI would
break the operator's own direct use of Hermes at a keyboard they are sitting
at, which is a different threat model from a message arriving over Telegram.
The CLI path is covered by the approval layer instead -- see
apply_approval_patch.py.
"""

from __future__ import annotations

import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
AGENTS = REPO / "agents"
PYTHON = AGENTS / ".venv" / "Scripts" / "python.exe"

# Exactly the tools each agent exposes. Kept here as a whitelist so a new tool
# on an agent is an explicit decision here, not an automatic grant.
AGENT_TOOLS = {
    "research": ["research"],
    "pm": ["pm_query", "pm_action"],
    "coding": ["start_code_task", "get_status", "get_result", "list_changed_files"],
}

TELEGRAM_TOOLSETS = [
    "clarify",
    "memory",
    "mcp-coding",
    "mcp-pm",
    "mcp-research",
    "session_search",
]

MARKER = "# juma-rebuild: agent MCP servers"


def hermes_home() -> Path:
    home = os.environ.get("HERMES_HOME", "").strip()
    if home:
        return Path(home)
    local = os.environ.get("LOCALAPPDATA", "")
    return (Path(local) if local else Path.home() / "AppData" / "Local") / "hermes"


def _yaml_list(items: list[str], indent: str) -> str:
    return "".join(f"{indent}- {item}\n" for item in items)


def build_mcp_block() -> str:
    lines = [
        MARKER,
        "# Each agent is a standalone process with its own model and its own .env.",
        "# Hermes passes no secrets: `env` below carries only what is needed to",
        "# start the interpreter, and each agent scrubs inherited credentials",
        "# before loading its own file (agents/_common/env.py).",
        "mcp_servers:",
    ]
    for name, tools in AGENT_TOOLS.items():
        lines += [
            f"  {name}:",
            f"    command: {PYTHON.as_posix()}",
            "    args:",
            f"      - {(AGENTS / name / 'main.py').as_posix()}",
            f"    cwd: {(AGENTS / name).as_posix()}",
            "    tools:",
            "      include:",
        ]
        lines += [f"        - {tool}" for tool in tools]
        lines += [
            "      resources: false",
            "      prompts: false",
        ]
    return "\n".join(lines) + "\n"


def _platform_block(lines: list[str], platform: str) -> tuple[int, int] | None:
    """Line range of one platform's toolset list, or None."""
    try:
        header = next(i for i, l in enumerate(lines) if l.startswith("platform_toolsets:"))
    except StopIteration:
        return None
    start = None
    for i in range(header + 1, len(lines)):
        stripped = lines[i].rstrip("\n")
        if stripped and not stripped.startswith(" "):
            break
        if stripped.strip() == f"{platform}:":
            start = i
            break
    if start is None:
        return None
    end = start + 1
    while end < len(lines) and lines[end].lstrip().startswith("- "):
        end += 1
    return start, end


def replace_telegram_toolsets(text: str) -> tuple[str, str]:
    lines = text.splitlines(keepends=True)
    span = _platform_block(lines, "telegram")
    if span is None:
        return text, "FAIL: platform_toolsets.telegram not found"
    start, end = span

    before = [l.strip()[2:] for l in lines[start + 1:end]]
    if before == TELEGRAM_TOOLSETS:
        return text, "SKIP: telegram toolsets already correct"

    removed = sorted(set(before) - set(TELEGRAM_TOOLSETS))
    new = lines[:start + 1] + [f"    - {t}\n" for t in TELEGRAM_TOOLSETS] + lines[end:]
    return "".join(new), f"SET telegram toolsets; removed: {', '.join(removed)}"


def add_agents_to_cli(text: str) -> tuple[str, str]:
    """Make the three agents reachable from the CLI as well.

    The CLI keeps terminal and file -- see the module docstring for why -- but
    the agents belong on every Hermes surface, not just Telegram. It also makes
    the Telegram configuration testable: with the agents registered, `hermes -z
    -t clarify,memory,session_search,mcp-research,mcp-pm,mcp-coding` reproduces
    the Telegram tool surface exactly, which is what the audit needs to probe.
    """
    lines = text.splitlines(keepends=True)
    span = _platform_block(lines, "cli")
    if span is None:
        return text, "FAIL: platform_toolsets.cli not found"
    start, end = span

    current = [l.strip()[2:] for l in lines[start + 1:end]]
    agents = [t for t in TELEGRAM_TOOLSETS if t.startswith("mcp-")]
    missing = [t for t in agents if t not in current]
    if not missing:
        return text, "SKIP: cli already has the agent toolsets"

    merged = sorted(set(current) | set(agents))
    new = lines[:start + 1] + [f"    - {t}\n" for t in merged] + lines[end:]
    return "".join(new), f"ADDED to cli: {', '.join(missing)}"


def main() -> int:
    config = hermes_home() / "config.yaml"
    if not config.exists():
        print(f"FAIL: {config} not found")
        return 1

    text = config.read_text(encoding="utf-8")
    backup = config.with_name(
        f"config.yaml.bak-juma-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(config, backup)
    print(f"backup: {backup.name}")

    text, message = replace_telegram_toolsets(text)
    print(f"  {message}")
    if message.startswith("FAIL"):
        return 1

    text, message = add_agents_to_cli(text)
    print(f"  {message}")
    if message.startswith("FAIL"):
        return 1

    if MARKER in text:
        head, _, tail = text.partition(MARKER)
        # Drop the previous block: everything from the marker to the next
        # top-level key that is not part of it.
        rest_lines = tail.splitlines(keepends=True)
        cut = 0
        for i, line in enumerate(rest_lines):
            if i == 0:
                continue
            if line.rstrip() and not line.startswith((" ", "#", "mcp_servers:")):
                cut = i
                break
        else:
            cut = len(rest_lines)
        text = head + "".join(rest_lines[cut:])
        print("  replaced the previous mcp_servers block")

    text = text.rstrip("\n") + "\n\n" + build_mcp_block()
    config.write_text(text, encoding="utf-8")
    print(f"  wrote mcp_servers for: {', '.join(AGENT_TOOLS)}")

    try:
        import yaml  # noqa: PLC0415
    except ImportError:
        print("OK (pyyaml not available here; config not re-parsed)")
        return 0

    parsed = yaml.safe_load(config.read_text(encoding="utf-8"))
    servers = parsed.get("mcp_servers") or {}
    telegram = (parsed.get("platform_toolsets") or {}).get("telegram") or []
    problems = []
    for name, tools in AGENT_TOOLS.items():
        entry = servers.get(name) or {}
        if sorted((entry.get("tools") or {}).get("include") or []) != sorted(tools):
            problems.append(f"{name}: include list wrong")
        if (entry.get("tools") or {}).get("resources") is not False:
            problems.append(f"{name}: resources not false")
        if (entry.get("tools") or {}).get("prompts") is not False:
            problems.append(f"{name}: prompts not false")
    for banned in ("terminal", "code_execution", "file", "computer_use", "browser"):
        if banned in telegram:
            problems.append(f"telegram still has {banned}")
    if problems:
        print("FAIL: " + "; ".join(problems))
        return 1
    print("OK: config re-parsed and verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
