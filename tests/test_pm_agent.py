#!/usr/bin/env python3
"""PM agent: the hardening, and the absence of delete.

Run: agents/.venv/Scripts/python tests/test_pm_agent.py
Add --live once agents/pm/.env holds a valid CLICKUP_TOKEN.

Most of this is offline and stays offline on purpose. The path-injection and
no-POST-retry properties are exactly the ones that cannot be tested against a
real workspace without creating duplicate tasks in it -- which is the bug.
"""

from __future__ import annotations

import json
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests"))
sys.path.insert(0, str(REPO / "agents"))
sys.path.insert(0, str(REPO / "agents" / "pm"))

from mcp_client import MCPStdioClient, python_exe  # noqa: E402

PY = python_exe()   # repo venv if present, else this interpreter
AGENT_PATH = str(REPO / "agents" / "pm" / "main.py")

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASSED if condition else FAILED).append(name if condition else f"{name}: {detail}")


# --------------------------------------------------------------------------
# Source-level: delete must not exist at all
# --------------------------------------------------------------------------

def test_no_delete_path() -> None:
    source = Path(AGENT_PATH).read_text(encoding="utf-8")

    check("no DELETE verb in the source",
          not re.search(r'["\']DELETE["\']', source),
          "a DELETE string literal is present")
    check("DELETE is absent from ALLOWED_METHODS",
          re.search(r"ALLOWED_METHODS\s*=\s*\{[^}]*\}", source)
          and "DELETE" not in re.search(r"ALLOWED_METHODS\s*=\s*\{[^}]*\}", source).group(0),
          "DELETE appears in ALLOWED_METHODS")
    for name in ("delete_task", "delete_list", "delete_space", "delete_comment",
                 "remove_task"):
        check(f"no {name} operation", f"def {name}" not in source, f"{name} is defined")

    import main as pm  # noqa: PLC0415

    check("ALLOWED_METHODS is exactly GET/POST/PUT",
          pm.ALLOWED_METHODS == {"GET", "POST", "PUT"}, str(pm.ALLOWED_METHODS))
    check("no operation offered to the model deletes",
          not any("delete" in t["function"]["name"].lower()
                  for t in pm.READ_OPS + pm.WRITE_OPS))
    check("ClickUp class has no delete method",
          not [m for m in dir(pm.ClickUp) if "delete" in m.lower() or "remove" in m.lower()],
          str([m for m in dir(pm.ClickUp) if "delete" in m.lower()]))

    client = pm.ClickUp("pk_1_TESTTOKENTESTTOKEN", "123")
    try:
        client._request("/task/abc", "DELETE")
        check("_request refuses DELETE", False, "it was accepted")
    except pm.AgentError as exc:
        check("_request refuses DELETE", exc.code == "method_not_allowed", exc.code)


# --------------------------------------------------------------------------
# Path injection
# --------------------------------------------------------------------------

def test_path_injection() -> None:
    import main as pm  # noqa: PLC0415

    hostile = [
        "../../team/123/space",
        "..%2f..%2fteam",
        "abc/../../x",
        "abc?foo=bar",
        "abc#frag",
        "abc def",
        "a" * 65,
        "",
        "../",
        "%2e%2e%2f",
    ]
    for value in hostile:
        try:
            pm.path_segment(value, "task_id")
            check(f"rejects id {value!r}", False, "it was accepted")
        except pm.AgentError as exc:
            check(f"rejects id {value!r}", exc.code == "bad_id", exc.code)

    for value in ("abc123", "8xyz-1_2", "900201234567"):
        try:
            check(f"accepts valid id {value!r}", pm.path_segment(value, "x") == value)
        except pm.AgentError as exc:
            check(f"accepts valid id {value!r}", False, exc.detail)


def test_error_redaction() -> None:
    import main as pm  # noqa: PLC0415

    body = json.dumps({
        "err": "Token pk_9999_SUPERSECRETTOKENVALUE is invalid for team",
        "ECODE": "OAUTH_027",
    })
    reason = pm.reason_for(401, body)
    check("error keeps the ECODE", reason == "ClickUp 401 (OAUTH_027)", reason)
    check("error drops the response body", "SUPERSECRET" not in reason, reason)

    plain = pm.reason_for(500, "Internal Server Error: trace at /opt/clickup/app.rb:88")
    check("non-JSON body is reduced to a status", plain == "ClickUp 500", plain)


# --------------------------------------------------------------------------
# Retry policy, against a loopback server
# --------------------------------------------------------------------------

class _FlakyHandler(BaseHTTPRequestHandler):
    hits: dict[str, int] = {}

    def _count(self) -> int:
        key = f"{self.command} {self.path}"
        _FlakyHandler.hits[key] = _FlakyHandler.hits.get(key, 0) + 1
        return _FlakyHandler.hits[key]

    def _respond(self) -> None:
        self._count()
        # Drain the request body before replying. Without this, a POST's unread
        # bytes stay in the socket and the NEXT request fails as a transport
        # error rather than the 500 this handler is meant to serve -- which
        # made the PUT-retry assertion below depend on socket buffer timing.
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        self.send_response(500)
        payload = json.dumps({"ECODE": "X_001"}).encode()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = do_PUT = _respond

    def log_message(self, *_args) -> None:
        pass


def test_post_is_never_retried() -> None:
    import main as pm  # noqa: PLC0415

    _FlakyHandler.hits = {}
    server = HTTPServer(("127.0.0.1", 0), _FlakyHandler)
    port = server.server_port
    threading.Thread(target=server.serve_forever, daemon=True).start()

    pm.ALLOWED_HOSTS.add("127.0.0.1")
    client = pm.ClickUp("pk_1_TESTTOKENTESTTOKEN", "123",
                        base=f"http://127.0.0.1:{port}/api/v2")

    try:
        client._request("/list/abc/task", "POST", {"name": "x"})
    except pm.AgentError:
        pass
    post_hits = _FlakyHandler.hits.get("POST /api/v2/list/abc/task", 0)
    check("a failing POST is attempted exactly once", post_hits == 1,
          f"it was sent {post_hits} times -- a retried create duplicates a task")

    try:
        client._request("/task/abc", "PUT", {"name": "x"})
    except pm.AgentError:
        pass
    put_hits = _FlakyHandler.hits.get("PUT /api/v2/task/abc", 0)
    check("a failing PUT is retried", put_hits > 1, f"sent {put_hits} times")

    server.shutdown()


# --------------------------------------------------------------------------
# The authority boundary is the tool list, checked in code
# --------------------------------------------------------------------------

def test_query_cannot_write() -> None:
    import main as pm  # noqa: PLC0415

    check("read ops and write ops do not overlap",
          not (pm.READ_NAMES & pm.WRITE_NAMES),
          str(pm.READ_NAMES & pm.WRITE_NAMES))
    check("write ops are create/update only",
          pm.WRITE_NAMES == {"create_task", "update_task", "create_list", "create_comment"},
          str(pm.WRITE_NAMES))

    # The loop dispatches only what `allowed` contains. Prove it by handing the
    # loop a model that insists on calling a write op inside a read-only run.
    class FakeLLM:
        def __init__(self):
            self.turn = 0

        def complete(self, **kwargs):
            self.turn += 1
            if self.turn == 1:
                return {"tool_calls": [{"id": "1", "function": {
                    "name": "create_task",
                    "arguments": json.dumps({"list_id": "abc", "name": "PWNED"})}}],
                    "text": "", "model": "fake"}
            return {"tool_calls": [], "text": "done", "model": "fake"}

    class FakeAudit:
        def __init__(self):
            self.events = []

        def write(self, event, **fields):
            self.events.append(event)

    class FakeBoot:
        llm = FakeLLM()
        audit = FakeAudit()

    class ExplodingClickUp:
        def create_task(self, **_kwargs):
            raise AssertionError("create_task was actually dispatched in a read-only run")

    out = pm.run_loop(FakeBoot(), ExplodingClickUp(), "sys", "do it",
                      pm.READ_OPS, pm.READ_NAMES)
    check("a write call inside pm_query is refused, not dispatched", True)
    check("the refusal is audited", "operation_refused" in FakeBoot.audit.events,
          str(FakeBoot.audit.events))
    check("no write was recorded as performed",
          not any(op["operation"] in pm.WRITE_NAMES for op in out["operations"]),
          str(out["operations"]))


# --------------------------------------------------------------------------
# Server level
# --------------------------------------------------------------------------

def _token_present() -> bool:
    env_file = REPO / "agents" / "pm" / ".env"
    if not env_file.exists():
        return False
    for line in env_file.read_text(encoding="utf-8-sig").splitlines():
        if line.strip().startswith("CLICKUP_TOKEN="):
            return bool(line.split("=", 1)[1].strip())
    return False


def test_server_tools() -> None:
    if not _token_present():
        print("  SKIP  server tests: agents/pm/.env has no CLICKUP_TOKEN yet")
        return
    with MCPStdioClient([PY, AGENT_PATH]) as client:
        tools = client.list_tools()
    names = sorted(t["name"] for t in tools)
    check("declares exactly pm_query and pm_action",
          names == ["pm_action", "pm_query"], str(names))
    for tool in tools:
        check(f"{tool['name']} declares it cannot delete",
              "delete" in (tool.get("description") or "").lower(),
              tool.get("description", ""))


def test_missing_token_fails_clearly() -> None:
    """A blank token must produce a diagnosable exit, not a silent hang."""
    import subprocess  # noqa: PLC0415

    if _token_present():
        print("  SKIP  missing-token test: a token is configured")
        return
    out = subprocess.run([PY, AGENT_PATH], capture_output=True, text=True,
                         timeout=60, input="")
    check("a blank CLICKUP_TOKEN exits non-zero", out.returncode != 0, str(out.returncode))
    check("the failure names the file", "agents/pm/.env" in out.stderr, out.stderr[-400:])
    check("the failure names the missing key", "CLICKUP_TOKEN" in out.stderr,
          out.stderr[-400:])
    check("nothing was written to stdout", out.stdout.strip() == "",
          f"stdout was {out.stdout[:200]!r}")


def test_live() -> None:
    with MCPStdioClient([PY, AGENT_PATH], timeout=240) as client:
        result = client.call("pm_query", {"question": "List my ClickUp spaces"})
    if not result.get("ok"):
        check("live pm_query succeeded", False, str(result)[:600])
        return
    check("live pm_query succeeded", True)
    check("live pm_query is marked read-only", result.get("read_only") is True)
    check("live pm_query returns an answer", bool(result.get("answer")))
    check("live pm_query performed only read operations",
          all(op["operation"] in
              {"list_spaces", "list_folders", "list_lists", "list_tasks",
               "get_task", "list_comments", "search_tasks"}
              for op in result.get("operations") or []),
          str(result.get("operations")))
    print("\n--- live pm_query answer ---")
    print((result.get("answer") or "")[:800])


def main() -> int:
    live = "--live" in sys.argv
    test_no_delete_path()
    test_path_injection()
    test_error_redaction()
    test_post_is_never_retried()
    test_query_cannot_write()
    test_server_tools()
    test_missing_token_fails_clearly()
    if live:
        test_live()

    for name in PASSED:
        print(f"  PASS  {name}")
    for name in FAILED:
        print(f"  FAIL  {name}")
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
