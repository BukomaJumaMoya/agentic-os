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

# MCP server name -> (agent directory, exactly the tools it exposes).
#
# THE SERVER NAME IS NOT FREE-FORM. Hermes merges MCP server names into the
# enabled-TOOLSET list (hermes_cli/tools_config.py, _merge_mcp_servers), and
# resolves each one as a toolset. So a server whose name matches a built-in
# toolset silently grants that entire toolset. `coding` is a built-in name --
# 37 tools including terminal, write_file and browser -- and naming the server
# `coding` is what put all of them on the Telegram surface while config.yaml
# still read as six restricted entries. See hermes/harden_telegram_surface.py.
#
# The server is therefore `coding_agent`; the directory stays `agents/coding`.
# Before adding a fourth agent, check its name against `hermes tools list`.
AGENT_SPEC = {
    "research": ("research", ["research"]),
    "pm": ("pm", ["pm_query", "pm_action"]),
    "coding_agent": ("coding", ["start_code_task", "get_status", "get_result",
                                "list_changed_files", "github_query",
                                "github_action", "docs_query"]),
}

# EVERY server is `trust: untrusted`, and that word does not mean what it looks
# like it means here. It is not a statement about whether these agents are
# trusted -- they are ours. It is the switch that turns on Hermes' per-tool
# human approval gate (tools/mcp_tool_handlers.py:_trust_gate_check): on an
# untrusted server, any tool whose discovery-time readOnlyHint is not exactly
# True must be approved by a human before the RPC is sent.
#
# That gate is DETERMINISTIC. It runs in Hermes' MCP layer, before the call
# leaves the process, and the model is not consulted and cannot skip it. The
# SOUL also asks Hermes to confirm before a write, but a prompt is a request;
# this is a check. Both are kept, because they fail differently.
#
# Which tools it catches is decided entirely by the annotations the agents
# publish -- see agents/coding/main.py, where the read tools carry
# readOnlyHint: True and start_code_task and github_action deliberately do not.
# Adding a write tool WITHOUT an annotation gates it automatically; adding one
# WITH readOnlyHint: True silently exempts it. That asymmetry is the right way
# round, and it is why the annotation is checked in the guard.
SERVER_TRUST = "untrusted"

# Kept as a name->tools view for the verification loop below.
AGENT_TOOLS = {name: tools for name, (_dir, tools) in AGENT_SPEC.items()}

# The three servers by their real names, so _merge_mcp_servers() treats this as
# an allowlist instead of falling back to "every globally enabled MCP server".
TELEGRAM_TOOLSETS = [
    "clarify",
    "memory",
    "session_search",
    "research",
    "pm",
    "coding_agent",
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
    for name, (directory, tools) in AGENT_SPEC.items():
        lines += [
            f"  {name}:",
            f"    command: {PYTHON.as_posix()}",
            "    args:",
            f"      - {(AGENTS / directory / 'main.py').as_posix()}",
            f"    cwd: {(AGENTS / directory).as_posix()}",
            f"    trust: {SERVER_TRUST}",
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
    the Telegram configuration testable: `hermes/cli_surface.py telegram`
    mirrors the Telegram list onto the CLI so `hermes -z` reproduces the
    Telegram tool surface exactly, which is what the audit needs to probe.
    """
    lines = text.splitlines(keepends=True)
    span = _platform_block(lines, "cli")
    if span is None:
        return text, "FAIL: platform_toolsets.cli not found"
    start, end = span

    current = [l.strip()[2:] for l in lines[start + 1:end]]
    agents = list(AGENT_SPEC)
    missing = [t for t in agents if t not in current]

    # mcp-research/mcp-pm/mcp-coding are neither MCP server names nor toolset
    # names. They resolve to nothing, which is how the whole surface came open:
    # a platform list naming no REAL server makes _merge_mcp_servers() fall back
    # to "merge every enabled server", and one of those collided with a built-in.
    # They are dead on the cli list too, and dead entries that read like grants
    # are exactly what nobody should have to re-derive next time.
    dead = [t for t in current if t.startswith("mcp-")]

    if not missing and not dead:
        return text, "SKIP: cli already has the agent toolsets"

    merged = sorted((set(current) | set(agents)) - set(dead))
    new = lines[:start + 1] + [f"    - {t}\n" for t in merged] + lines[end:]
    changes = []
    if missing:
        changes.append(f"added {', '.join(missing)}")
    if dead:
        changes.append(f"removed dead {', '.join(sorted(dead))}")
    return "".join(new), "CLI: " + "; ".join(changes)


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
        if entry.get("trust") != SERVER_TRUST:
            problems.append(f"{name}: trust is {entry.get('trust')!r}, not "
                            f"{SERVER_TRUST!r} -- write approval would be off")
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
