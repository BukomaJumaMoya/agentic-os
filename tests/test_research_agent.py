#!/usr/bin/env python3
"""Research agent, exercised over real MCP stdio.

Run: agents/.venv/Scripts/python tests/test_research_agent.py
Add --live to include the two tests that spend a Tavily credit and a model call.

The offline tests are the ones that matter for containment; the live ones prove
the thing actually works end to end.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests"))
sys.path.insert(0, str(REPO / "agents"))

from mcp_client import MCPStdioClient  # noqa: E402

PY = str(REPO / "agents" / ".venv" / "Scripts" / "python.exe")
AGENT = str(REPO / "agents" / "research" / "main.py")

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASSED if condition else FAILED).append(name if condition else f"{name}: {detail}")


def test_server_starts_and_declares_one_tool() -> None:
    with MCPStdioClient([PY, AGENT]) as client:
        tools = client.list_tools()
    names = sorted(t["name"] for t in tools)
    check("server completes the MCP handshake", True)
    check("declares exactly the research tool", names == ["research"], str(names))

    tool = tools[0]
    schema = tool.get("inputSchema", {})
    props = set((schema.get("properties") or {}).keys())
    check("research takes question and depth", props == {"question", "depth"}, str(props))
    check("description says it is read-only",
          "read-only" in (tool.get("description") or "").lower(),
          tool.get("description", ""))


def test_bad_input_is_structured_not_a_crash() -> None:
    with MCPStdioClient([PY, AGENT]) as client:
        empty = client.call("research", {"question": "  ", "depth": "quick"})
        check("empty question is rejected", empty.get("ok") is False, str(empty))
        check("empty question uses a stable code",
              empty.get("error") == "bad_input", str(empty))

        bad_depth = client.call("research", {"question": "x", "depth": "exhaustive"})
        check("unknown depth is rejected", bad_depth.get("ok") is False, str(bad_depth))
        check("unknown depth names the valid values",
              "quick" in bad_depth.get("detail", ""), str(bad_depth))

        long_q = client.call("research", {"question": "x" * 5000, "depth": "quick"})
        check("over-long question is rejected", long_q.get("ok") is False, str(long_q))

        for result in (empty, bad_depth, long_q):
            check("no traceback in error", "Traceback" not in str(result), str(result))


def test_no_secrets_and_no_stray_stdout() -> None:
    with MCPStdioClient([PY, AGENT]) as client:
        client.list_tools()
        result = client.call("research", {"question": "", "depth": "quick"})
        stderr = client.stderr
    check("startup writes nothing secret to stderr",
          "sk-or-v1-" not in stderr and "tvly-" not in stderr,
          "a key appeared in stderr")
    check("stdout stayed valid JSON-RPC", isinstance(result, dict))


def test_env_isolation() -> None:
    """A ClickUp token in the environment must not survive into the process."""
    env = dict(os.environ)
    env["CLICKUP_TOKEN"] = "pk_00000_INHERITEDSHOULDBEGONE"
    env["SOME_OTHER_SECRET"] = "inherited-should-be-gone"

    probe = (
        "import sys, os; sys.path.insert(0, r'" + str(REPO / "agents") + "');"
        "from _common import env as e; e.load('research',"
        "required=['OPENROUTER_API_KEY','TAVILY_API_KEY']);"
        "print('CLICKUP_TOKEN' in os.environ, 'SOME_OTHER_SECRET' in os.environ)"
    )
    out = subprocess.run([PY, "-c", probe], capture_output=True, text=True,
                         env=env, timeout=60)
    check("env probe ran", out.returncode == 0, out.stderr[-500:])
    check("inherited CLICKUP_TOKEN is scrubbed", "False False" in out.stdout,
          f"stdout={out.stdout!r}")


def test_network_allowlist() -> None:
    probe = (
        "import sys; sys.path.insert(0, r'" + str(REPO / "agents") + "');"
        "sys.path.insert(0, r'" + str(REPO / "agents" / "research") + "');"
        "import main;"
        "from _common.errors import AgentError\n"
        "try:\n"
        "    main._post('https://api.clickup.com/api/v2/team', {}, {}, 5)\n"
        "    print('REACHED')\n"
        "except AgentError as exc:\n"
        "    print('BLOCKED', exc.code)\n"
    )
    out = subprocess.run([PY, "-c", probe], capture_output=True, text=True, timeout=60)
    check("ClickUp is not reachable from the research agent",
          "BLOCKED host_not_allowed" in out.stdout,
          f"stdout={out.stdout!r} stderr={out.stderr[-400:]}")


def test_no_write_capability() -> None:
    """The agent has no filesystem or process primitive in its source at all.

    A capability check, not a style check: the claim "read only, no file
    writes, no shell" is only worth making if something verifies it, and the
    cheapest thing that does is the absence of the primitives.
    """
    import re

    source = Path(AGENT).read_text(encoding="utf-8")
    forbidden = {
        # \b...\b so urlopen( does not read as open(.
        "builtin open()": r"(?<![\w.])open\s*\(",
        "subprocess": r"\bsubprocess\b",
        "os.system": r"\bos\.system\b",
        "os.popen": r"\bos\.popen\b",
        "shutil": r"\bshutil\b",
        "write_text/write_bytes": r"\.write_(?:text|bytes)\s*\(",
        "mkdir": r"\.mkdir\s*\(",
        "os.remove/unlink": r"\bos\.(?:remove|unlink|rmdir)\b",
    }
    for label, pattern in forbidden.items():
        hits = re.findall(pattern, source)
        check(f"agent source contains no {label}", not hits,
              f"found {hits[:3]} in agents/research/main.py")


def test_live(depth: str = "quick") -> None:
    with MCPStdioClient([PY, AGENT], timeout=240) as client:
        result = client.call(
            "research",
            {"question": "What is the Model Context Protocol?", "depth": depth},
        )
    if not result.get("ok"):
        check("live research succeeded", False, str(result)[:600])
        return
    check("live research succeeded", True)
    check("live research returns a summary", bool(result.get("summary")), str(result)[:300])
    check("live research cites sources", len(result.get("sources") or []) > 0)
    check("sources carry urls",
          all(str(s.get("url", "")).startswith("http") for s in result["sources"]))
    check("summary contains a citation marker", "[1]" in (result.get("summary") or ""),
          (result.get("summary") or "")[:300])
    check("sources are labelled untrusted",
          all(s.get("trust") == "untrusted-third-party-content" for s in result["sources"]))
    print("\n--- live summary (first 500 chars) ---")
    print((result.get("summary") or "")[:500])
    print(f"--- {len(result['sources'])} sources ---")
    for source in result["sources"][:5]:
        print(f"  [{source['n']}] {source['url']}")


def main() -> int:
    live = "--live" in sys.argv
    test_server_starts_and_declares_one_tool()
    test_bad_input_is_structured_not_a_crash()
    test_no_secrets_and_no_stray_stdout()
    test_env_isolation()
    test_network_allowlist()
    test_no_write_capability()
    if live:
        test_live()

    for name in PASSED:
        print(f"  PASS  {name}")
    for name in FAILED:
        print(f"  FAIL  {name}")
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed"
          + ("" if live else "  (offline only; pass --live for a real search)"))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
