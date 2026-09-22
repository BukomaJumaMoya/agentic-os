#!/usr/bin/env python3
"""Fail if anything but the orchestrator's own tools reaches the Telegram surface.

WHAT THIS DEFENDS AGAINST
-------------------------
The README's central claim is that Hermes "has no terminal, no file access, no
code execution and no browser". That claim was false in the running system for
an unknown period, and nothing said so. The cause was not a setting anyone
changed -- see hermes/harden_telegram_surface.py for the full trace -- it was a
NAME COLLISION that appears at tool-resolution time and is invisible in
config.yaml, where every line still reads correctly.

That is the dangerous shape: a confinement boundary that is asserted in a
config file, enforced somewhere else, and never compared against itself. So it
is compared here, against the resolver Hermes actually uses, and the gateway
refuses to start when they disagree.

IT MUST RUN UNDER HERMES' OWN INTERPRETER
-----------------------------------------
    ~/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe \
        hermes/check_telegram_surface.py

because it imports Hermes' resolver rather than reimplementing it. A
reimplementation would drift from the thing it is checking, which is how the
original defect survived: two descriptions of the same surface, never compared.

WHAT IT CANNOT SEE
------------------
MCP tools only exist once their stdio servers have connected, which does not
happen in a bare check process. So this asserts one direction only: that
NOTHING OUTSIDE THE ALLOWLIST is present. A missing MCP tool is a broken
feature and shows up immediately in use; a present `terminal` is a breached
boundary and shows up only when someone goes looking. Those deserve different
treatment, and the fail-closed direction is the one that matters here.
"""

from __future__ import annotations

import sys

PLATFORM = "telegram"

# Hermes' own built-ins the orchestrator is allowed to keep. `clarify` asks the
# operator a question, `memory` and `session_search` read its own history. None
# of the three can touch the filesystem, the network or a shell.
ALLOWED_BUILTINS = {"clarify", "memory", "session_search"}

# The three agents' tools, allowed by name so a new tool on an agent has to be
# an explicit decision here rather than an automatic grant -- the same rule
# configure_orchestrator.py applies to the MCP include lists.
ALLOWED_MCP_TOOLS = {
    "research",
    "pm_query", "pm_action",
    "start_code_task", "get_status", "get_result", "list_changed_files",
}

# The tool_search bridge. Permitted but not desired: it means the agent tools
# were deferred behind a lookup, which costs an extra round trip per turn.
BRIDGE_TOOLS = {"tool_search", "tool_describe", "tool_call"}

ALLOWED = ALLOWED_BUILTINS | ALLOWED_MCP_TOOLS | BRIDGE_TOOLS


def resolve_telegram_tools():
    """(tool names, resolved toolset names) for the Telegram platform."""
    from hermes_cli.config import load_config
    from hermes_cli.tools_config import _get_platform_tools
    import model_tools

    cfg = load_config() or {}
    toolsets = sorted(_get_platform_tools(cfg, PLATFORM))
    agent_cfg = cfg.get("agent") or {}
    try:
        from agent.skill_utils import parse_config_string_list
        disabled = parse_config_string_list(agent_cfg.get("disabled_toolsets")) or None
    except Exception:
        disabled = None
    names = sorted(model_tools._select_tool_names(toolsets, disabled, True))
    return names, toolsets


def check():
    """(ok, message). Never raises: the caller decides what a failure means."""
    try:
        names, toolsets = resolve_telegram_tools()
    except Exception as exc:
        # Cannot prove the surface is safe -> treat as unsafe.
        return False, (f"could not resolve the {PLATFORM} tool surface "
                       f"({type(exc).__name__}: {exc}); refusing to assume it is safe")

    forbidden = sorted(set(names) - ALLOWED)
    if forbidden:
        return False, (
            f"{len(forbidden)} forbidden tool(s) on the {PLATFORM} surface: "
            f"{', '.join(forbidden)}. Resolved toolsets: {toolsets}. "
            f"Run hermes/harden_telegram_surface.py.")
    return True, (f"{PLATFORM} surface clean: {len(names)} built-in tool(s) "
                  f"{names}; resolved toolsets {toolsets}")


def main() -> int:
    ok, message = check()
    print(("OK: " if ok else "FAIL: ") + message)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
