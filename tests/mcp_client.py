#!/usr/bin/env python3
"""A minimal MCP stdio client, for testing agents the way Hermes calls them.

Speaking the real protocol matters here. A test that imports an agent's module
and calls the Python function tests the function; it does not test that the
server starts, that its tool schemas are well-formed, that its stdout is clean
JSON-RPC, or that a missing key produces a diagnosable failure rather than a
silent hang. Those are the failures that actually happen with stdio servers,
and they are invisible to an in-process test.

Usage:
    from mcp_client import MCPStdioClient
    with MCPStdioClient([python, "agents/research/main.py"]) as client:
        print(client.list_tools())
        print(client.call("research", {"question": "...", "depth": "quick"}))
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
import sys
import threading
from typing import Any


class MCPError(RuntimeError):
    pass


class MCPStdioClient:
    def __init__(self, command: list[str], *, cwd: str | None = None,
                 env: dict | None = None, timeout: float = 180.0):
        self.command = command
        self.cwd = cwd
        self.env = env
        self.timeout = timeout
        self._id = 0
        self._proc: subprocess.Popen | None = None
        self._stderr: list[str] = []

    def __enter__(self) -> "MCPStdioClient":
        self._proc = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=self.cwd, env=self.env, text=True, encoding="utf-8",
            errors="replace", bufsize=1,
        )
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        self._handshake()
        return self

    def __exit__(self, *_exc: Any) -> None:
        if self._proc:
            try:
                self._proc.stdin.close()
            except Exception:
                pass
            try:
                self._proc.wait(timeout=5)
            except Exception:
                self._proc.kill()

    @property
    def stderr(self) -> str:
        return "".join(self._stderr)

    def _drain_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        for line in self._proc.stderr:
            self._stderr.append(line)

    def _send(self, method: str, params: dict | None = None,
              *, notify: bool = False) -> Any:
        assert self._proc and self._proc.stdin
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        if not notify:
            self._id += 1
            message["id"] = self._id
        self._proc.stdin.write(json.dumps(message) + "\n")
        self._proc.stdin.flush()
        if notify:
            return None
        return self._read_reply(message["id"])

    def _read_reply(self, want_id: int) -> Any:
        assert self._proc and self._proc.stdout
        result: list[Any] = []

        def reader() -> None:
            for line in self._proc.stdout:  # type: ignore[union-attr]
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    # A non-JSON line on stdout is a real bug in the server:
                    # it corrupts the protocol stream. Surface it, do not skip.
                    result.append(MCPError(f"non-JSON on stdout: {line[:300]!r}"))
                    return
                if message.get("id") == want_id:
                    result.append(message)
                    return

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        thread.join(self.timeout)
        if not result:
            raise MCPError(
                f"no reply to request {want_id} within {self.timeout}s. "
                f"server stderr:\n{self.stderr[-3000:]}"
            )
        reply = result[0]
        if isinstance(reply, MCPError):
            raise reply
        if "error" in reply:
            raise MCPError(f"server returned error: {reply['error']}")
        return reply.get("result")

    def _handshake(self) -> None:
        self._send("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "juma-test-client", "version": "1.0"},
        })
        self._send("notifications/initialized", {}, notify=True)

    def list_tools(self) -> list[dict]:
        return (self._send("tools/list", {}) or {}).get("tools", [])

    def call(self, name: str, arguments: dict) -> dict:
        result = self._send("tools/call", {"name": name, "arguments": arguments}) or {}
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            # MCPServer wraps a non-dict return under "result"; unwrap it.
            if set(structured) == {"result"}:
                return structured["result"]
            return structured
        for block in result.get("content", []):
            if block.get("type") == "text":
                try:
                    return json.loads(block["text"])
                except Exception:
                    return {"_text": block["text"]}
        return result


def python_exe() -> str:
    """The interpreter the agents should run under, on any platform.

    The repo venv if it exists (Windows `Scripts/`, POSIX `bin/`), otherwise
    the interpreter running this test. The tests used to hardcode
    `agents/.venv/Scripts/python.exe`, which meant CI -- Ubuntu, no venv --
    died with FileNotFoundError on the first agent it tried to start. That is
    the second reason `quality` had never passed; the credential scan was the
    first.
    """
    import os
    repo = Path(__file__).resolve().parent.parent
    # Only the interpreter for THIS platform. Existence alone is not enough: a
    # Windows checkout mounted into a Linux container still contains
    # agents/.venv/Scripts/python.exe, which exists and cannot run. The agent
    # then produced no output at all and every stdio test timed out after 180
    # seconds against an empty stderr, which is a miserable thing to debug.
    candidate = (repo / "agents" / ".venv" / "Scripts" / "python.exe"
                 if os.name == "nt"
                 else repo / "agents" / ".venv" / "bin" / "python")
    return str(candidate) if candidate.exists() else sys.executable
