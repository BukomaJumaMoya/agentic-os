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

THE EIGHT CONDITIONS
--------------------
One tool-surface check was not enough, because the surface is only one of the
things that has to hold before the gateway is allowed to talk to Telegram. All
eight fail CLOSED, and a check that raises counts as failed:

  1. tool surface      nothing outside the manifest is on the wire
  2. approval patch    reading the Hermes secret store still asks
  3. command_allowlist still empty -- a non-empty one pre-approves commands
  4. telegram allow    exactly one authorised user, and still the same one
  5. MCP include lists config.yaml agrees with hermes/surface-manifest.json
  6. surface ⊆ manifest every resolved tool has a declared owner
  7. write approval    every MCP server is trust: untrusted, so Hermes' own
                       per-tool gate stops a write before its RPC is sent
  8. gate sees hints   what Hermes COMPUTED about each tool's readOnlyHint
                       matches the manifest -- the comparison that was missing

4 compares a SHA-256, not the ID. The ID is the entire authentication boundary
and this file is in a repository; a digest pins the value without publishing it,
and a changed ID still fails the check.

WHAT IT CANNOT SEE
------------------
MCP tools only exist once their stdio servers have connected, which does not
happen in a bare check process. So check 1 asserts one direction only: that
NOTHING OUTSIDE THE ALLOWLIST is present. A missing MCP tool is a broken
feature and shows up immediately in use; a present `terminal` is a breached
boundary and shows up only when someone goes looking. Those deserve different
treatment, and the fail-closed direction is the one that matters here.

Check 5 covers the other direction from config: an include list that grew a
tool the manifest does not declare fails even though the tool never resolves in
this process.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

PLATFORM = "telegram"

# EVERY surface, not just the one that was audited.
#
# platform_toolsets had no `cron` entry, so Hermes fell back to the `cli` list
# and every scheduled job ran with terminal, write_file and execute_code --
# while this guard watched `telegram` and reported it clean. Checking one
# surface and calling it "the surface" is the exact mistake that produced the
# original collision.
#
# `cli` is deliberately NOT here: it is an operator at their own keyboard, a
# different threat model, and it is covered by the approval patch instead.
GUARDED_PLATFORMS = ("telegram", "cron")
REPO = Path(__file__).resolve().parent.parent
MANIFEST_PATH = REPO / "hermes" / "surface-manifest.json"

# Hermes' own built-ins the orchestrator is allowed to keep. `clarify` asks the
# operator a question, `memory` and `session_search` read its own history. None
# of the three can touch the filesystem, the network or a shell. They are
# declared in the manifest like everything else, owned by "hermes-builtin".
BUILTIN_OWNER = "hermes-builtin"

# The tool_search bridge. Permitted but not desired: it means the agent tools
# were deferred behind a lookup, which costs an extra round trip per turn. Not
# in the manifest because no agent owns them and they carry no capability.
BRIDGE_TOOLS = {"tool_search", "tool_describe", "tool_call"}

APPROVAL_MARKER = "juma-rebuild: Hermes secret-store READ rules"


# --------------------------------------------------------------------------
# inputs
# --------------------------------------------------------------------------

def hermes_home() -> Path:
    home = os.environ.get("HERMES_HOME", "").strip()
    if home:
        return Path(home)
    local = os.environ.get("LOCALAPPDATA", "")
    return (Path(local) if local else Path.home() / "AppData" / "Local") / "hermes"


def load_manifest(path: Path | None = None) -> dict:
    return json.loads((path or MANIFEST_PATH).read_text(encoding="utf-8"))


def manifest_tools(manifest: dict) -> dict[str, str]:
    """{tool name: owner}. Raises if the file is not shaped as expected --
    an unreadable manifest must not read as an empty one."""
    tools = manifest.get("tools")
    if not isinstance(tools, dict) or not tools:
        raise ValueError("manifest has no 'tools' map")
    for name, owner in tools.items():
        if not isinstance(owner, str) or not owner.strip():
            raise ValueError(f"manifest tool {name!r} has no owner")
    return dict(tools)


def manifest_servers(manifest: dict) -> dict[str, set[str]]:
    """{MCP server: its tools}, built by grouping the manifest by owner.
    The built-ins are not a server and are excluded."""
    servers: dict[str, set[str]] = {}
    for tool, owner in manifest_tools(manifest).items():
        if owner == BUILTIN_OWNER:
            continue
        servers.setdefault(owner, set()).add(tool)
    return servers


def hermes_env(path: Path | None = None) -> dict[str, str]:
    """Hermes' own .env, parsed for NAMES and values we compare by digest.
    Nothing here is ever printed."""
    envfile = path or (hermes_home() / ".env")
    found: dict[str, str] = {}
    if not envfile.exists():
        return found
    for line in envfile.read_text(encoding="utf-8", errors="replace").splitlines():
        match = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$", line)
        if match:
            found[match.group(1)] = match.group(2).strip().strip('"').strip("'")
    return found


def digest(value: str) -> str:
    return hashlib.sha256(value.strip().encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# the eight checks -- each pure enough to be called with doctored input
# --------------------------------------------------------------------------

def resolve_telegram_tools(cfg, platform: str = PLATFORM):
    """(tool names, resolved toolset names) for one platform."""
    from hermes_cli.tools_config import _get_platform_tools
    import model_tools

    toolsets = sorted(_get_platform_tools(cfg, platform))
    agent_cfg = cfg.get("agent") or {}
    try:
        from agent.skill_utils import parse_config_string_list
        disabled = parse_config_string_list(agent_cfg.get("disabled_toolsets")) or None
    except Exception:
        disabled = None
    names = sorted(model_tools._select_tool_names(toolsets, disabled, True))
    return names, toolsets


def check_tool_surface(names, toolsets, manifest, platform=PLATFORM) -> tuple[bool, str]:
    """1. Nothing on the wire that the manifest does not declare."""
    allowed = set(manifest_tools(manifest)) | BRIDGE_TOOLS
    forbidden = sorted(set(names) - allowed)
    if forbidden:
        return False, (
            f"{len(forbidden)} forbidden tool(s) on the {platform} surface: "
            f"{', '.join(forbidden)}. Resolved toolsets: {toolsets}. "
            f"Run hermes/harden_telegram_surface.py.")
    return True, f"{len(names)} resolved tool(s) {names}, all declared"


def check_surface_in_manifest(names, manifest, platform=PLATFORM) -> tuple[bool, str]:
    """6. Every resolved tool has a declared owner.

    Distinct from check 1 in what it says, not in what it computes: 1 is the
    security verdict ("a forbidden tool is present"), 6 is the bookkeeping one
    ("this tool is on the surface and nothing claims it"). The bridge tools are
    the only exemption and they are named, not inferred.
    """
    declared = manifest_tools(manifest)
    undeclared = sorted(set(names) - set(declared) - BRIDGE_TOOLS)
    if undeclared:
        return False, (f"on the {platform} surface but absent from "
                       f"{MANIFEST_PATH.name}: {', '.join(undeclared)}")
    return True, f"all {len(names)} surface tool(s) have an owner"


def check_approval_patch(target: Path | None = None) -> tuple[bool, str]:
    """2. Reading %LOCALAPPDATA%\hermes\.env still requires approval.

    `hermes update` stashes local changes to its own checkout, which is exactly
    how this patch disappeared once before -- silently, with the agent running
    and `cat %LOCALAPPDATA%\hermes\.env` back to a verdict of `allow`.
    """
    path = target or (hermes_home() / "hermes-agent" / "tools" / "approval_detection.py")
    if not path.exists():
        return False, f"{path} not found; cannot prove the approval patch is applied"
    if APPROVAL_MARKER not in path.read_text(encoding="utf-8", errors="replace"):
        return False, (f"the approval patch is missing from {path.name} -- reading "
                       f"the Hermes secret store no longer asks. Run "
                       f"hermes/apply_approval_patch.py.")
    return True, "applied"


def check_command_allowlist(cfg) -> tuple[bool, str]:
    """3. Empty. Every entry is a command pre-approved without a prompt."""
    allowlist = cfg.get("command_allowlist")
    if allowlist is None:
        return False, "command_allowlist is absent; expected an empty list"
    if allowlist != []:
        return False, (f"command_allowlist has {len(allowlist)} entry/entries "
                       f"{allowlist}; every one is a command that runs without "
                       f"asking. It must be empty.")
    return True, "empty"


def check_telegram_allow_list(cfg, manifest, env=None) -> tuple[bool, str]:
    """4. Exactly one authorised Telegram user, and still the same one.

    Compared by SHA-256 against the digest pinned in the manifest, so the ID
    itself is in neither this file nor the repository. Both places that carry
    it are checked: config.yaml is what the gateway enforces, and
    TELEGRAM_ALLOWED_USERS is what the operator edits.
    """
    expected = manifest.get("telegram_allowed_users_sha256")
    if not expected:
        return False, ("manifest has no telegram_allowed_users_sha256; cannot "
                       "prove the allow-list is unchanged")

    platforms = ((cfg.get("gateway") or {}).get("platforms") or {})
    configured = (platforms.get(PLATFORM) or {}).get("allow_list")
    if not isinstance(configured, list) or len(configured) != 1:
        count = "absent" if configured is None else f"{len(configured or [])} entries"
        return False, (f"gateway.platforms.{PLATFORM}.allow_list must hold exactly "
                       f"one user id; found {count}")
    if digest(str(configured[0])) != expected:
        return False, (f"gateway.platforms.{PLATFORM}.allow_list holds a user id "
                       f"that is not the authorised one (digest mismatch)")

    values = env if env is not None else hermes_env()
    declared = (values.get("TELEGRAM_ALLOWED_USERS") or "").strip()
    if not declared:
        return False, "TELEGRAM_ALLOWED_USERS is not set in Hermes' .env"
    if "," in declared:
        return False, (f"TELEGRAM_ALLOWED_USERS lists {len(declared.split(','))} "
                       f"users; it must name exactly one")
    if digest(declared) != expected:
        return False, "TELEGRAM_ALLOWED_USERS is not the authorised user (digest mismatch)"
    return True, "exactly one user, digest matches"


def manifest_write_tools(manifest: dict) -> set[str]:
    """Tools the manifest declares as changing the outside world.

    Raises when the key is absent: a missing write list would make every write
    look read-only, which is the direction that silently removes approval.
    """
    writes = manifest.get("write_tools")
    if not isinstance(writes, list) or not writes:
        raise ValueError("manifest has no 'write_tools' list")
    return set(writes)


def check_write_approval_armed(cfg, manifest) -> tuple[bool, str]:
    """7. Every MCP server is `trust: untrusted`, so write approval is on.

    This one line of config is the entire deterministic approval gate. Without
    it, Hermes calls every agent tool straight through and the only thing
    standing between a Telegram message and a real ClickUp write, a commit, or
    a pull request is the model having read its SOUL carefully.

    `full` is Hermes' compat default, so an mcp_servers block written by hand,
    or regenerated by an older script, arrives with approval OFF and nothing
    says so. Hence: checked on every start, not assumed from having set it once.
    """
    servers = cfg.get("mcp_servers") or {}
    if not servers:
        return False, "no mcp_servers are configured"
    ungated = sorted(name for name, entry in servers.items()
                     if str((entry or {}).get("trust", "")).strip().lower()
                     != "untrusted")
    if ungated:
        return False, (f"write approval is OFF for {', '.join(ungated)} "
                       f"(trust is not 'untrusted'). Every write tool on "
                       f"{'that server' if len(ungated) == 1 else 'those servers'} "
                       f"would run without asking. Run "
                       f"hermes/configure_orchestrator.py.")

    writes = manifest_write_tools(manifest)
    declared = set(manifest_tools(manifest))
    undeclared = sorted(writes - declared)
    if undeclared:
        return False, (f"manifest write_tools names {', '.join(undeclared)}, "
                       f"which is not in the tools map")
    return True, (f"{len(servers)} server(s) trust: untrusted; "
                  f"{len(writes)} write tool(s) gated: {', '.join(sorted(writes))}")


def cached_hints(path: Path | None = None) -> dict[str, bool]:
    """{tool: readOnlyHint} as HERMES COMPUTED IT, from its discovery cache.

    Absent cache -> {} -> check 8 passes, because Hermes rebuilds it on the next
    connect and there is nothing yet to disagree with.
    """
    cache = path or (hermes_home() / "cache" / "mcp_schema_cache.json")
    if not cache.exists():
        return {}
    data = json.loads(cache.read_text(encoding="utf-8"))
    found: dict[str, bool] = {}

    def walk(node):
        if isinstance(node, dict):
            if node.get("name") and "annotations" in node:
                hint = (node.get("annotations") or {}).get("readOnlyHint")
                found[node["name"]] = hint is True
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return found


def check_gate_sees_annotations(manifest, hints=None) -> tuple[bool, str]:
    """8. What Hermes computed about each tool matches what we declared.

    THE CHECK THAT WAS MISSING. Two others already existed:
      - tests assert what the AGENTS publish on the wire
      - a probe asserts what the GATE does with hints it is handed
    Neither covered the step between, and that step was broken:
    `_annotation_read_only_hint()` read `readOnlyHint` off a pydantic model
    whose attribute is `read_only_hint`, so every hint came back None and every
    read tool was classified write-capable. Both checks stayed green. The daily
    briefing died on its first real run because nothing unattended can approve.

    So this compares Hermes' OWN computed answer against the manifest. It is
    the same lesson as the toolset collision: two correct descriptions of one
    surface are worth nothing until something compares them.
    """
    computed = cached_hints() if hints is None else hints
    if not computed:
        return True, "no discovery cache yet (rebuilt on next connect)"

    writes = manifest_write_tools(manifest)
    declared = set(manifest_tools(manifest))
    wrong = []
    for tool, is_read_only in sorted(computed.items()):
        if tool not in declared:
            continue
        should_be_read_only = tool not in writes
        if is_read_only != should_be_read_only:
            wrong.append(
                f"{tool}: Hermes thinks readOnlyHint={is_read_only}, "
                f"manifest says it is a {'write' if tool in writes else 'read'} tool")
    if wrong:
        return False, ("Hermes' tool annotations disagree with the manifest -- "
                       + "; ".join(wrong)
                       + ". A read tool seen as write-capable blocks every "
                         "unattended run; a write tool seen as read-only loses "
                         "its approval prompt. Run hermes/patch_readonly_hint.py.")
    checked = len(set(computed) & declared)
    return True, f"{checked} cached annotation(s) agree with the manifest"


def check_mcp_includes(cfg, manifest) -> tuple[bool, str]:
    """5. config.yaml's include lists and the manifest say the same thing.

    Both directions. A server in config that the manifest does not know about
    is an undeclared agent; a server in the manifest that config does not
    define is a manifest describing a system that is not running.
    """
    declared = manifest_servers(manifest)
    configured = {
        name: set(((entry.get("tools") or {}).get("include")) or [])
        for name, entry in (cfg.get("mcp_servers") or {}).items()
    }
    problems = []
    for name in sorted(set(declared) | set(configured)):
        want, have = declared.get(name), configured.get(name)
        if want is None:
            problems.append(f"{name}: in config.yaml, absent from the manifest")
        elif have is None:
            problems.append(f"{name}: in the manifest, absent from config.yaml")
        elif want != have:
            extra = sorted(have - want)
            missing = sorted(want - have)
            detail = []
            if extra:
                detail.append(f"undeclared {extra}")
            if missing:
                detail.append(f"declared but not included {missing}")
            problems.append(f"{name}: " + ", ".join(detail))
    if problems:
        return False, ("config.yaml and the manifest disagree -- "
                       + "; ".join(problems))
    return True, f"{len(declared)} server(s) match the manifest"


# --------------------------------------------------------------------------

def check():
    """(ok, message). Never raises: the caller decides what a failure means."""
    try:
        from hermes_cli.config import load_config
        cfg = load_config() or {}
        manifest = load_manifest()
        surfaces = {p: resolve_telegram_tools(cfg, p) for p in GUARDED_PLATFORMS}
        names, toolsets = surfaces[PLATFORM]
    except Exception as exc:
        # Cannot prove the surface is safe -> treat as unsafe.
        return False, (f"could not load the {PLATFORM} configuration "
                       f"({type(exc).__name__}: {exc}); refusing to assume it is safe")

    checks = []
    for _p in GUARDED_PLATFORMS:
        _n, _ts = surfaces[_p]
        checks.append((f"{_p} surface",
                       lambda n=_n, ts=_ts, p=_p: check_tool_surface(n, ts, manifest, p)))
        checks.append((f"{_p} in manifest",
                       lambda n=_n, p=_p: check_surface_in_manifest(n, manifest, p)))
    checks += [
        ("approval patch", check_approval_patch),
        ("command_allowlist", lambda: check_command_allowlist(cfg)),
        ("telegram allow-list", lambda: check_telegram_allow_list(cfg, manifest)),
        ("mcp include lists", lambda: check_mcp_includes(cfg, manifest)),
        ("write approval armed", lambda: check_write_approval_armed(cfg, manifest)),
        ("gate sees annotations", lambda: check_gate_sees_annotations(manifest)),
    ]

    failures, notes = [], []
    for label, fn in checks:
        try:
            ok, message = fn()
        except Exception as exc:
            # A check that cannot run has not passed.
            ok, message = False, f"check raised {type(exc).__name__}: {exc}"
        (notes if ok else failures).append(f"{label}: {message}")

    if failures:
        return False, " | ".join(failures)
    return True, " | ".join(notes)


def main() -> int:
    ok, message = check()
    print(("OK: " if ok else "FAIL: ") + message)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
