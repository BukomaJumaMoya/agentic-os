#!/usr/bin/env python3
"""Temporarily make the CLI present the same tool surface as Telegram.

WHY THIS IS NEEDED TO TEST AT ALL
---------------------------------
The audit questions are about the Telegram surface: "can Hermes run a shell
command if asked", "does a coding instruction reach the coding agent". Telegram
messages cannot be injected locally, so the tests run through `hermes -z`, which
uses the `cli` platform's toolsets -- a different, much larger set that still
includes terminal and file. Probing that would answer a question nobody asked.

There is no flag that fixes this. `-t` cannot name an MCP toolset: those
register only after their stdio servers connect, which happens long after
argument validation, so `-t mcp-research` is dropped with "ignoring unknown
--toolsets entries" and the run silently proceeds without the agents.

A second, blunter reason: the full CLI toolset does not FIT. Its tool schemas
overflow a 131k context window, and every Groq model is 131k. The same prompt
on the six-toolset Telegram surface fits comfortably.

So: swap `cli` to the Telegram list, run the tests, swap it back.

    python hermes/cli_surface.py telegram   # mirror Telegram onto cli
    python hermes/cli_surface.py restore    # put the real cli list back

The previous list is saved beside config.yaml, so restore does not depend on
this process still being alive.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

STASH_NAME = "cli_toolsets.stash.json"


def hermes_home() -> Path:
    home = os.environ.get("HERMES_HOME", "").strip()
    if home:
        return Path(home)
    local = os.environ.get("LOCALAPPDATA", "")
    return (Path(local) if local else Path.home() / "AppData" / "Local") / "hermes"


def main() -> int:
    import yaml

    mode = (sys.argv[1] if len(sys.argv) > 1 else "").lower()
    if mode not in ("telegram", "restore"):
        print(__doc__)
        return 2

    home = hermes_home()
    config_path = home / "config.yaml"
    stash_path = home / STASH_NAME
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    toolsets = config["platform_toolsets"]

    if mode == "telegram":
        if stash_path.exists():
            print(f"SKIP: already swapped (stash present at {stash_path.name})")
            return 0
        stash_path.write_text(json.dumps(toolsets["cli"]), encoding="utf-8")
        toolsets["cli"] = list(toolsets["telegram"])
        print(f"cli -> {toolsets['cli']}")
        print(f"previous cli list stashed in {stash_path.name}")
    else:
        if not stash_path.exists():
            print("SKIP: no stash to restore from")
            return 0
        toolsets["cli"] = json.loads(stash_path.read_text(encoding="utf-8"))
        stash_path.unlink()
        print(f"cli restored -> {toolsets['cli']}")

    config_path.write_text(
        yaml.safe_dump(config, sort_keys=False, allow_unicode=True, width=100),
        encoding="utf-8")

    check = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    telegram = check["platform_toolsets"]["telegram"]
    for banned in ("terminal", "code_execution", "file", "computer_use", "browser"):
        if banned in telegram:
            print(f"FAIL: telegram regained {banned}")
            return 1
    print("OK: telegram surface unchanged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
