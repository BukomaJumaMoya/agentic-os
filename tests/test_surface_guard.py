#!/usr/bin/env python3
"""Negative tests for the startup guard: prove it REFUSES.

Run: agents/.venv/Scripts/python tests/test_surface_guard.py

WHY THESE AND NOT A PASSING RUN
-------------------------------
`hermes/check_telegram_surface.py` returning OK on a healthy machine proves
almost nothing. A function that returns `(True, "")` unconditionally passes that
test too, and so does one whose condition can never be false. The guard exists
for the day something is wrong, so what has to be tested is the day something is
wrong: one deliberately broken input per condition, each asserted to fail AND to
say why.

This is also the rule in documentation/OPERATING-RULES.md: anything touching the
invariants re-runs these, not the positive check.

Each condition is a pure function taking the config, the manifest or a path, so
a broken case is a doctored dict rather than a broken machine. Nothing here
edits config.yaml, Hermes' checkout or the manifest -- a test that has to break
the running system to prove a guard works will eventually be run by someone who
forgets to put it back.

The one thing these cannot reach is Hermes' own tool resolver, which needs
Hermes' interpreter and a connected gateway. That direction is covered by
check_tool_surface() being handed a resolved name list directly, and by the
end-to-end refusal recorded in documentation/AUDIT-agentic-os.md.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "hermes"))

import check_telegram_surface as guard  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
    else:
        FAILED.append(f"{name}: {detail}")


def refuses(name: str, result, *, mentioning: str = "") -> None:
    """Assert a check failed, and that its message is diagnosable.

    The message matters as much as the verdict: a guard that refuses with
    "error" sends whoever is on the other end of it to read the source.
    """
    ok, message = result
    check(f"{name} -- refuses", ok is False, f"returned ok={ok!r} ({message})")
    check(f"{name} -- says why", bool(message and len(message) > 20),
          f"message was {message!r}")
    if mentioning:
        check(f"{name} -- names the problem", mentioning.lower() in message.lower(),
              f"{mentioning!r} not in {message!r}")


# The healthy inputs every test starts from and then breaks exactly one part of.
GOOD_MANIFEST = guard.load_manifest()
GOOD_TOOLS = sorted(guard.manifest_tools(GOOD_MANIFEST))
GOOD_SURFACE = ["clarify", "memory", "session_search"]

GOOD_CONFIG = {
    "command_allowlist": [],
    "gateway": {"platforms": {"telegram": {"allow_list": [4242424242]}}},
    "mcp_servers": {
        name: {"tools": {"include": sorted(tools)}}
        for name, tools in guard.manifest_servers(GOOD_MANIFEST).items()
    },
}
# The fixture's allow-list id is invented, so the manifest digest is replaced
# with that id's digest for the tests that are not ABOUT the allow-list.
GOOD_MANIFEST_FOR_CONFIG = dict(
    GOOD_MANIFEST,
    telegram_allowed_users_sha256=guard.digest("4242424242"))
GOOD_ENV = {"TELEGRAM_ALLOWED_USERS": "4242424242"}


def copy(obj):
    return json.loads(json.dumps(obj))


# --------------------------------------------------------------------------
# condition 1 -- a forbidden tool on the wire
# --------------------------------------------------------------------------

def test_forbidden_tool_on_surface() -> None:
    """The original defect: an MCP server named after a built-in toolset
    granted that whole toolset, and 24 tools reached the model."""
    breached = GOOD_SURFACE + ["terminal", "write_file", "execute_code"]
    refuses("forbidden tool on the surface",
            guard.check_tool_surface(breached, ["coding"], GOOD_MANIFEST),
            mentioning="terminal")

    check("a clean surface still passes",
          guard.check_tool_surface(GOOD_SURFACE, ["memory"], GOOD_MANIFEST)[0] is True,
          "the healthy case was refused, so the check refuses everything")


# --------------------------------------------------------------------------
# condition 2 -- the approval patch is missing
# --------------------------------------------------------------------------

def test_approval_patch_missing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        unpatched = Path(tmp) / "approval_detection.py"
        unpatched.write_text("DANGEROUS_PATTERNS = []\n", encoding="utf-8")
        refuses("approval patch stashed away by `hermes update`",
                guard.check_approval_patch(unpatched),
                mentioning="approval patch is missing")

        absent = Path(tmp) / "does-not-exist.py"
        refuses("approval detection file absent entirely",
                guard.check_approval_patch(absent),
                mentioning="not found")

        patched = Path(tmp) / "patched.py"
        patched.write_text(f"# {guard.APPROVAL_MARKER}\n", encoding="utf-8")
        check("a patched file passes",
              guard.check_approval_patch(patched)[0] is True,
              "the healthy case was refused")


# --------------------------------------------------------------------------
# condition 3 -- command_allowlist is not empty
# --------------------------------------------------------------------------

def test_command_allowlist_not_empty() -> None:
    cfg = copy(GOOD_CONFIG)
    cfg["command_allowlist"] = ["git status"]
    refuses("command_allowlist has an entry",
            guard.check_command_allowlist(cfg), mentioning="must be empty")

    cfg = copy(GOOD_CONFIG)
    del cfg["command_allowlist"]
    refuses("command_allowlist removed from config",
            guard.check_command_allowlist(cfg), mentioning="absent")

    check("an empty allowlist passes",
          guard.check_command_allowlist(GOOD_CONFIG)[0] is True,
          "the healthy case was refused")


# --------------------------------------------------------------------------
# condition 4 -- the Telegram allow-list is not exactly the one user
# --------------------------------------------------------------------------

def test_telegram_allow_list() -> None:
    manifest = GOOD_MANIFEST_FOR_CONFIG

    cfg = copy(GOOD_CONFIG)
    cfg["gateway"]["platforms"]["telegram"]["allow_list"] = [4242424242, 999000111]
    refuses("a second user added to the allow-list",
            guard.check_telegram_allow_list(cfg, manifest, GOOD_ENV),
            mentioning="exactly one")

    cfg = copy(GOOD_CONFIG)
    cfg["gateway"]["platforms"]["telegram"]["allow_list"] = [999000111]
    refuses("the allow-list user swapped for another",
            guard.check_telegram_allow_list(cfg, manifest, GOOD_ENV),
            mentioning="digest mismatch")

    cfg = copy(GOOD_CONFIG)
    cfg["gateway"]["platforms"]["telegram"]["allow_list"] = []
    refuses("the allow-list emptied",
            guard.check_telegram_allow_list(cfg, manifest, GOOD_ENV),
            mentioning="exactly one")

    refuses("TELEGRAM_ALLOWED_USERS names a different user",
            guard.check_telegram_allow_list(GOOD_CONFIG, manifest,
                                            {"TELEGRAM_ALLOWED_USERS": "999000111"}),
            mentioning="digest mismatch")

    refuses("TELEGRAM_ALLOWED_USERS lists two users",
            guard.check_telegram_allow_list(
                GOOD_CONFIG, manifest,
                {"TELEGRAM_ALLOWED_USERS": "4242424242,999000111"}),
            mentioning="exactly one")

    refuses("TELEGRAM_ALLOWED_USERS unset",
            guard.check_telegram_allow_list(GOOD_CONFIG, manifest, {}),
            mentioning="not set")

    unpinned = {k: v for k, v in manifest.items()
                if k != "telegram_allowed_users_sha256"}
    refuses("the manifest lost its pinned digest",
            guard.check_telegram_allow_list(GOOD_CONFIG, unpinned, GOOD_ENV),
            mentioning="cannot prove")

    check("the authorised user passes",
          guard.check_telegram_allow_list(GOOD_CONFIG, manifest, GOOD_ENV)[0] is True,
          "the healthy case was refused")


# --------------------------------------------------------------------------
# condition 5 -- an include list disagrees with the manifest
# --------------------------------------------------------------------------

def test_mcp_includes_disagree() -> None:
    cfg = copy(GOOD_CONFIG)
    cfg["mcp_servers"]["pm"]["tools"]["include"].append("pm_delete")
    refuses("an agent grew a tool nobody declared",
            guard.check_mcp_includes(cfg, GOOD_MANIFEST), mentioning="pm_delete")

    cfg = copy(GOOD_CONFIG)
    cfg["mcp_servers"]["research"]["tools"]["include"] = []
    refuses("an include list emptied behind the manifest's back",
            guard.check_mcp_includes(cfg, GOOD_MANIFEST), mentioning="research")

    cfg = copy(GOOD_CONFIG)
    cfg["mcp_servers"]["shell_agent"] = {"tools": {"include": ["run_command"]}}
    refuses("a fourth MCP server added without a manifest entry",
            guard.check_mcp_includes(cfg, GOOD_MANIFEST),
            mentioning="absent from the manifest")

    cfg = copy(GOOD_CONFIG)
    del cfg["mcp_servers"]["coding_agent"]
    refuses("a declared server missing from config",
            guard.check_mcp_includes(cfg, GOOD_MANIFEST),
            mentioning="absent from config.yaml")

    check("matching include lists pass",
          guard.check_mcp_includes(GOOD_CONFIG, GOOD_MANIFEST)[0] is True,
          "the healthy case was refused")


# --------------------------------------------------------------------------
# condition 6 -- a surface tool with no owner
# --------------------------------------------------------------------------

def test_surface_tool_not_in_manifest() -> None:
    refuses("an undeclared tool reached the surface",
            guard.check_surface_in_manifest(GOOD_SURFACE + ["delegate_task"],
                                            GOOD_MANIFEST),
            mentioning="delegate_task")

    check("the bridge tools are exempt by name",
          guard.check_surface_in_manifest(
              GOOD_SURFACE + sorted(guard.BRIDGE_TOOLS), GOOD_MANIFEST)[0] is True,
          "tool_search was treated as undeclared")

    check("every manifest tool is accepted on the surface",
          guard.check_surface_in_manifest(GOOD_TOOLS, GOOD_MANIFEST)[0] is True,
          "the healthy case was refused")


# --------------------------------------------------------------------------
# the fail-closed contract itself
# --------------------------------------------------------------------------

def test_unreadable_manifest_fails_closed() -> None:
    """An unreadable manifest must not read as an empty one.

    This is the direction that decides whether the guard is a guard: if a
    malformed file produced an empty tool map, every check downstream would
    compare against nothing and pass.
    """
    with tempfile.TemporaryDirectory() as tmp:
        empty = Path(tmp) / "m.json"
        empty.write_text("{}", encoding="utf-8")
        raised = False
        try:
            guard.manifest_tools(guard.load_manifest(empty))
        except Exception:
            raised = True
        check("a manifest with no tools map raises rather than returning {}",
              raised, "manifest_tools() accepted an empty manifest")

        ownerless = Path(tmp) / "n.json"
        ownerless.write_text('{"tools": {"research": ""}}', encoding="utf-8")
        raised = False
        try:
            guard.manifest_tools(guard.load_manifest(ownerless))
        except Exception:
            raised = True
        check("a tool with a blank owner raises", raised,
              "manifest_tools() accepted a tool with no owner")


def test_broken_check_counts_as_failure() -> None:
    """check() wraps every condition: one that raises is a refusal, not a skip."""
    source = (REPO / "hermes" / "check_telegram_surface.py").read_text(encoding="utf-8")
    check("check() treats a raising condition as failed",
          "check raised" in source and "(notes if ok else failures)" in source,
          "the per-check try/except is gone; a raising check would be skipped")
    check("check() reports failures, not the notes, when anything failed",
          'return False, " | ".join(failures)' in source,
          "a failure message would be diluted with the passing checks")


def main() -> int:
    for func in (test_forbidden_tool_on_surface,
                 test_approval_patch_missing,
                 test_command_allowlist_not_empty,
                 test_telegram_allow_list,
                 test_mcp_includes_disagree,
                 test_surface_tool_not_in_manifest,
                 test_unreadable_manifest_fails_closed,
                 test_broken_check_counts_as_failure):
        func()

    for name in PASSED:
        print(f"  PASS  {name}")
    for name in FAILED:
        print(f"  FAIL  {name}")
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
