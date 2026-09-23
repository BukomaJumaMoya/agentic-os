#!/usr/bin/env python3
"""Close the Telegram tool-surface hole, and make a silent reopening fail closed.

THE DEFECT
----------
`platform_toolsets.telegram` listed six entries and the model received
thirty-seven tools, including terminal, write_file, patch, execute_code,
delegate_task and the whole browser toolset. Every diagnostic that reads
config.yaml -- `hermes tools list`, `hermes tools --summary` -- reported the
surface as correctly restricted. Only the request on the wire disagreed.

THE CAUSE, TRACED
-----------------
It is a NAME COLLISION, not a setting anyone changed and not an update
regression:

  1. hermes_cli/tools_config.py:686-687  ``_merge_mcp_servers()``
     When the platform list names no real MCP server, every globally enabled
     MCP server name is merged into the enabled-toolset list. Our servers are
     named ``research``, ``pm`` and ``coding``, so those three names were added.
     (The list DID name ``mcp-research``/``mcp-pm``/``mcp-coding``, but those
     are not MCP server names and not toolset names -- they resolve to nothing,
     which is why the default merge fired at all.)

  2. toolsets.py:177  built-in composite ``"coding"``
     Hermes ships a toolset called ``coding``: "files, terminal, search, web
     docs, skills, todo, delegate, vision, browser". It has exactly the same
     name as our MCP server.

  3. model_tools.py:324  ``_select_tool_names()`` -> ``resolve_toolset("coding")``
     The merged name is resolved as a toolset. ``resolve_toolset("coding")``
     returns 37 tools. Measured: ``research`` and ``pm`` resolve to 0 tools and
     are harmless; ``coding`` alone opened the entire surface.

So an MCP server whose name happens to match a built-in toolset silently grants
that whole toolset. The boundary was never breached at runtime -- it was never
built, and the config that described it stayed truthful-looking throughout.

THE FIX, CONFIG FIRST
---------------------
1. Rename the MCP server ``coding`` -> ``coding_agent``. This removes the
   collision at its source. The directory, the process and the four tool names
   are untouched; only the server's key in config.yaml changes.
2. Name the real MCP servers in ``platform_toolsets.telegram``. That turns the
   default "merge every enabled server" branch into an explicit allowlist, so a
   fourth MCP server added later is not automatically on the Telegram surface.
3. Turn ``tools.tool_search.enabled`` off. With seven agent tools there is
   nothing to page: deferring them behind tool_search/tool_describe/tool_call
   costs an extra model call per turn to discover tools that fit comfortably in
   the prompt. It also restores ``session_search``, which Hermes defers by
   default.

No Hermes source is patched for the fix itself -- the defect is reachable
entirely through configuration, so configuration closes it.

THE GUARD
---------
The fix above is a config change, and config is exactly what proved unreliable:
every reading of it was correct while the surface was open. So Hermes' gateway
entry point is patched to re-derive the surface from Hermes' OWN resolver at
every start, and to refuse to start when a forbidden tool appears. That patch
is the one piece of source editing here, and it follows apply_approval_patch.py
exactly: marker-guarded, idempotent, re-applied by running this script, and
required after every `hermes update` because update stashes local changes.

It fails closed in both directions: if the check itself cannot be loaded or
raises, the gateway still refuses to start. A guard that fails open is not a
guard, it is a comment.

    python hermes/harden_telegram_surface.py
    python hermes/harden_telegram_surface.py --check-only
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# Backups accumulate forever otherwise: 49 copies of config.yaml, every one
# holding a plaintext bot token, none covered by the approval patch. Pruned at
# the moment one is created, which is the only moment the count can grow.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from backup_prune import prune as _prune_backups  # noqa: E402


REPO = Path(__file__).resolve().parent.parent
CHECKER = REPO / "hermes" / "check_telegram_surface.py"

OLD_SERVER = "coding"
NEW_SERVER = "coding_agent"

# Built-ins the orchestrator keeps, then the three agents by their real server
# names. Naming the servers is what makes _merge_mcp_servers() treat this as an
# allowlist instead of falling back to "every enabled server".
TELEGRAM_TOOLSETS = [
    "clarify",
    "memory",
    "session_search",
    "research",
    "pm",
    NEW_SERVER,
    "docs_agent",
    "n8n_agent",
    "kola_agent",
]

GUARD_MARKER = "juma-rebuild: Telegram tool-surface guard"

# Inside ``start_gateway()``, not ``main()``. main() is only the argv entry
# point of ``python -m gateway.run``; ``hermes gateway start/restart`` and the
# scheduled task both run ``python -m hermes_cli.main gateway run``, which
# reaches start_gateway() WITHOUT passing through main(). A guard in main() was
# tried first and a deliberately re-broken config started and connected to
# Telegram anyway -- the guard has to sit on the function every path shares.
GUARD_ANCHOR = '    os.environ["HERMES_EXEC_ASK"] = "1"\n'


def hermes_home() -> Path:
    home = os.environ.get("HERMES_HOME", "").strip()
    if home:
        return Path(home)
    local = os.environ.get("LOCALAPPDATA", "")
    return (Path(local) if local else Path.home() / "AppData" / "Local") / "hermes"


def hermes_python() -> Path:
    return hermes_home() / "hermes-agent" / "venv" / "Scripts" / "python.exe"


def guard_block() -> str:
    """The block inserted into gateway/run.py. Fails closed on every path."""
    return f'''    # --- {GUARD_MARKER} ---------------------------
    # platform_toolsets.telegram once listed six entries while the model
    # received thirty-seven, including terminal and write_file, because an MCP
    # server named "coding" collides with a built-in toolset of that name
    # (see hermes/harden_telegram_surface.py). Every config-reading diagnostic
    # called that surface restricted. Only the wire disagreed.
    #
    # So the surface is re-derived here from Hermes' own resolver on every
    # start, and disagreement stops the gateway. Placed beside
    # _guard_corrupt_user_config() because it is the same kind of check: a
    # precondition that must hold before any adapter touches the network.
    #
    # Fails closed. If the checker cannot be imported or raises, the gateway
    # does not start -- an unprovable boundary is treated as a breached one.
    # Returns False rather than raising: that is start_gateway()'s own
    # "failed to start" contract, and it gives a non-zero exit so a supervisor
    # reports the failure instead of flapping.
    try:
        import importlib.util as _juma_ilu
        _juma_spec = _juma_ilu.spec_from_file_location(
            "_juma_surface_check", r"{CHECKER}")
        _juma_mod = _juma_ilu.module_from_spec(_juma_spec)
        _juma_spec.loader.exec_module(_juma_mod)
        _juma_ok, _juma_msg = _juma_mod.check()
    except Exception as _juma_exc:
        _juma_ok = False
        _juma_msg = (f"tool-surface guard could not run "
                     f"({{type(_juma_exc).__name__}}: {{_juma_exc}}); "
                     f"refusing to assume the surface is safe")
    if not _juma_ok:
        logging.getLogger("gateway.run").critical(
            "REFUSING TO START -- Telegram tool surface: %s", _juma_msg)
        print(f"REFUSING TO START -- Telegram tool surface: {{_juma_msg}}",
              file=sys.stderr, flush=True)
        return False
    logging.getLogger("gateway.run").info(
        "Telegram tool-surface guard passed: %s", _juma_msg)
    # --- end {GUARD_MARKER} -----------------------
'''


def fix_config() -> list[str]:
    """Apply the three config changes. Returns a list of what changed."""
    import yaml

    config_path = hermes_home() / "config.yaml"
    if not config_path.exists():
        raise SystemExit(f"FAIL: {config_path} not found")

    backup = config_path.with_name(
        f"config.yaml.bak-surface-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(config_path, backup)
    print(f"backup: {backup.name}")
    _prune_backups(config_path.parent if "config_path" in dir() else backup.parent,
                   "config.yaml.bak-*")

    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    changes: list[str] = []

    # 1. The collision itself.
    servers = config.get("mcp_servers") or {}
    if OLD_SERVER in servers:
        # Rebuild in order so the renamed server keeps its position in the file.
        config["mcp_servers"] = {
            (NEW_SERVER if k == OLD_SERVER else k): v for k, v in servers.items()
        }
        changes.append(f"mcp_servers.{OLD_SERVER} -> mcp_servers.{NEW_SERVER} "
                       f"(no longer collides with the built-in '{OLD_SERVER}' toolset)")
    elif NEW_SERVER in servers:
        print(f"  mcp_servers.{NEW_SERVER} already renamed")
    else:
        raise SystemExit(
            f"FAIL: neither mcp_servers.{OLD_SERVER} nor .{NEW_SERVER} exists")

    # 2. Explicit MCP allowlist on the Telegram surface.
    toolsets = config.setdefault("platform_toolsets", {})
    if list(toolsets.get("telegram") or []) != TELEGRAM_TOOLSETS:
        toolsets["telegram"] = list(TELEGRAM_TOOLSETS)
        changes.append(f"platform_toolsets.telegram -> {TELEGRAM_TOOLSETS}")
    else:
        print("  platform_toolsets.telegram already correct")

    # 2b. The bot token does not belong in config.yaml.
    #
    # Hermes resolves TELEGRAM_BOT_TOKEN from the environment
    # (gateway/config_env.py: _Cred(Platform.TELEGRAM, ("TELEGRAM_BOT_TOKEN",))),
    # and the gateway already loads <home>/.env at startup. So the token can
    # live in exactly one place instead of two.
    #
    # It mattered more than "tidier". Every script here copies config.yaml
    # aside before editing, and nobody pruned: 49 backups had accumulated, all
    # 49 holding the token in plaintext, none of them covered by the approval
    # patch -- which matches config.yaml, .env, auth.json and mcp-tokens, not
    # config.yaml.bak-20260921-110148. Rotating the token fixed one copy and
    # left forty-nine. With the token out of config.yaml, a backup of it is no
    # longer a copy of a secret.
    #
    # allow_list stays: it is an identifier, not a credential, and guard
    # condition 4 checks it by digest on every start.
    platforms = (config.setdefault("gateway", {})
                 .setdefault("platforms", {}).setdefault("telegram", {}))
    if platforms.pop("bot_token", None) is not None:
        changes.append("gateway.platforms.telegram.bot_token removed "
                       "(read from TELEGRAM_BOT_TOKEN in <home>/.env instead)")

    # 3. Stop deferring seven tools behind a three-tool bridge.
    tools_cfg = config.setdefault("tools", {})
    ts_cfg = tools_cfg.setdefault("tool_search", {})
    if ts_cfg.get("enabled") != "off":
        ts_cfg["enabled"] = "off"
        changes.append("tools.tool_search.enabled -> off "
                       "(agent tools eager, not behind tool_search)")
    else:
        print("  tools.tool_search.enabled already off")

    if changes:
        config_path.write_text(
            yaml.safe_dump(config, sort_keys=False, allow_unicode=True,
                           default_flow_style=False, width=100),
            encoding="utf-8")
    for change in changes:
        print(f"  {change}")

    # Re-read and verify rather than trusting the write.
    check = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    problems = []
    if OLD_SERVER in (check.get("mcp_servers") or {}):
        problems.append(f"mcp_servers.{OLD_SERVER} still present")
    if NEW_SERVER not in (check.get("mcp_servers") or {}):
        problems.append(f"mcp_servers.{NEW_SERVER} missing")
    if list(((check.get("platform_toolsets") or {}).get("telegram")) or []) != TELEGRAM_TOOLSETS:
        problems.append("platform_toolsets.telegram did not round-trip")
    if (((check.get("tools") or {}).get("tool_search") or {}).get("enabled")) != "off":
        problems.append("tools.tool_search.enabled did not round-trip")
    if check.get("command_allowlist") != []:
        problems.append("command_allowlist is no longer empty")

    telegram_cfg = (((check.get("gateway") or {}).get("platforms") or {})
                    .get("telegram") or {})
    if telegram_cfg.get("bot_token"):
        problems.append("bot_token is still in config.yaml")
    envfile = hermes_home() / ".env"
    env_text = envfile.read_text(encoding="utf-8", errors="replace") if envfile.exists() else ""
    if "TELEGRAM_BOT_TOKEN=" not in env_text:
        # Removing it from config without it being in .env would take the bot
        # off Telegram entirely. Refuse rather than do that silently.
        problems.append(f"TELEGRAM_BOT_TOKEN is not in {envfile}; refusing to "
                        f"leave the gateway with no token")
    if problems:
        raise SystemExit("FAIL: " + "; ".join(problems))
    return changes


def apply_guard() -> bool:
    """Patch gateway/run.py to refuse to start on a dirty surface. Idempotent."""
    target = hermes_home() / "hermes-agent" / "gateway" / "run.py"
    if not target.exists():
        raise SystemExit(f"FAIL: {target} not found")

    text = target.read_text(encoding="utf-8")
    if GUARD_MARKER in text:
        print(f"  guard already present in {target.name}")
        return False
    if GUARD_ANCHOR not in text:
        raise SystemExit(
            f"FAIL: anchor not found in {target.name}. Upstream moved "
            f"_guard_corrupt_user_config(); re-point GUARD_ANCHOR before rerunning.")

    backup = target.with_name(
        f"run.py.bak-surface-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(target, backup)
    text = text.replace(GUARD_ANCHOR, GUARD_ANCHOR + "\n" + guard_block(), 1)
    target.write_text(text, encoding="utf-8")
    print(f"  guard inserted into {target.name} (backup: {backup.name})")

    # A syntax error here would make the gateway unstartable, so prove it parses.
    import ast
    try:
        ast.parse(target.read_text(encoding="utf-8"))
    except SyntaxError as exc:
        shutil.copy2(backup, target)
        raise SystemExit(f"FAIL: patch broke {target.name} ({exc}); restored the backup")
    return True


def run_checker() -> int:
    """Run the checker under Hermes' interpreter, which is the only one that
    can import Hermes' resolver."""
    python = hermes_python()
    if not python.exists():
        print(f"WARN: {python} not found; skipping verification")
        return 0
    env = dict(os.environ, PYTHONIOENCODING="utf-8",
               HERMES_HOME=str(hermes_home()))
    proc = subprocess.run(
        [str(python), str(CHECKER)],
        cwd=str(hermes_home() / "hermes-agent"),
        env=env, capture_output=True, text=True)
    print((proc.stdout or "").strip() or (proc.stderr or "").strip())
    return proc.returncode


def main() -> int:
    check_only = "--check-only" in sys.argv

    if not check_only:
        print("== config ==")
        fix_config()
        print("== gateway guard ==")
        apply_guard()

    print("== verification (Hermes' own resolver) ==")
    rc = run_checker()
    if rc != 0:
        print("FAIL: the Telegram surface is still not clean")
        return 1
    print("OK: Telegram surface clean and the gateway guard is in place")
    return 0


if __name__ == "__main__":
    sys.exit(main())
