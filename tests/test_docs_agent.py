#!/usr/bin/env python3
"""Tests for the docs agent: identity is code-authored and it cannot send.

Run: agents/.venv/Scripts/python tests/test_docs_agent.py
     ... --live   also drafts a real proposal through a model

The offline tests are the ones that matter. `check_proposal_invariants` is the
gate that would have caught a draft signed "Alex Mercer" with a stale dateline,
and it is tested by handing it exactly that.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents" / "docs"))
sys.path.insert(0, str(REPO / "tests"))

import invariants  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASSED if condition else FAILED).append(name if condition else f"{name}: {detail}")


def test_identity_is_loadable() -> None:
    identity = invariants.load_identity()
    check("config/juma.json loads", bool(identity), "empty identity")
    for field in ("name", "business", "default_rate"):
        check(f"identity has {field}", bool(identity.get(field)), str(identity.get(field)))


def test_invariants_catch_the_archived_failure() -> None:
    identity = invariants.load_identity()

    good = (f"Prepared by {identity['name']} of {identity['business']}.\n"
            "Thank you for your enquiry.")
    result = invariants.check_proposal_invariants({}, good, identity)
    check("a correct proposal passes", result["ok"], str(result["violations"]))

    # The exact archived failure: wrong signatory, no business name.
    bad = invariants.check_proposal_invariants({}, "Prepared by Alex Mercer.", identity)
    fields = {v["field"] for v in bad["violations"]}
    check("a wrong signatory is caught", "name" in fields, str(fields))
    check("a missing business name is caught", "business" in fields, str(fields))

    # A rate the config never set.
    rate = invariants.check_proposal_invariants(
        {}, f"Prepared by {identity['name']} of {identity['business']}. Rate $500/hr.",
        identity)
    check("a rate that contradicts config is caught",
          any(v["field"] == "default_rate" for v in rate["violations"]),
          str(rate["violations"]))

    # Fail closed on an unreadable config: it must not silently approve.
    empty = invariants.check_proposal_invariants({}, "anything at all", {})
    check("an empty config refuses rather than passes",
          not empty["ok"] and empty["violations"][0]["field"] == "config",
          str(empty))


def test_agent_cannot_send_anything() -> None:
    """The containment is the absence of a code path, not a prompt rule."""
    source = (REPO / "agents" / "docs" / "main.py").read_text(encoding="utf-8")
    for forbidden in ("smtplib", "sendgrid", "api.telegram.org", "TELEGRAM_BOT_TOKEN",
                      "CLICKUP_TOKEN", "requests.post", "urllib.request.urlopen"):
        check(f"docs agent has no {forbidden}", forbidden not in source,
              f"{forbidden} appears in main.py")

    env = REPO / "agents" / "docs" / ".env"
    if env.exists():
        names = [line.split("=", 1)[0].strip()
                 for line in env.read_text(encoding="utf-8").splitlines()
                 if "=" in line and not line.strip().startswith("#")]
        for forbidden in ("CLICKUP_TOKEN", "TELEGRAM_BOT_TOKEN", "TAVILY_API_KEY",
                          "GITHUB_TOKEN", "KOLA_API_KEY"):
            check(f"docs .env does not hold {forbidden}", forbidden not in names,
                  f"names present: {names}")


def test_server_tools() -> None:
    from mcp_client import MCPStdioClient
    python = REPO / "agents" / ".venv" / "Scripts" / "python.exe"
    main = REPO / "agents" / "docs" / "main.py"
    with MCPStdioClient([str(python), str(main)], cwd=str(main.parent),
                        timeout=180) as client:
        tools = client.list_tools()
    names = sorted(t["name"] for t in tools)
    check("declares exactly draft_proposal", names == ["draft_proposal"], str(names))
    annotations = {t["name"]: (t.get("annotations") or {}) for t in tools}
    check("draft_proposal is annotated read-only",
          annotations.get("draft_proposal", {}).get("readOnlyHint") is True,
          "it writes only a local PDF, so it needs no approval prompt")


def main() -> int:
    for func in (test_identity_is_loadable,
                 test_invariants_catch_the_archived_failure,
                 test_agent_cannot_send_anything,
                 test_server_tools):
        func()
    for name in PASSED:
        print(f"  PASS  {name}")
    for name in FAILED:
        print(f"  FAIL  {name}")
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
