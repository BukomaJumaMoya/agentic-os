#!/usr/bin/env python3
"""Let an agent be an MCP *client*, so a third-party server never touches Hermes.

WHY AN AGENT AND NOT HERMES
---------------------------
Hermes is the one component with no confinement of its own: it has no terminal,
no files and no shell precisely because everything it can reach is meant to be
behind an agent boundary. Attaching a write-capable third-party MCP server
directly to Hermes would put a tool that can change the outside world on the
same surface as the router, with no policy between them and nobody's audit log
recording the call.

So a third-party server is connected HERE, by the agent that owns it. The agent
applies its own policy, writes its own audit line, and exposes to Hermes only
its own narrow tools. Hermes sees `github_query`; it never sees `create_issue`,
`merge_pull_request` or the ninety other tools the upstream server offers.

There is a second, duller reason, and on this system it binds first. Hermes'
per-call prompt is metered against a free tier that allows 250,000 input tokens
a DAY. Its whole tool surface is ~6.6k tokens per call. A server with 122 tools
attached directly would multiply that; behind an agent it costs Hermes two tool
schemas regardless of how many tools the upstream has.

THE POLICY THIS ENFORCES
------------------------
1. An ALLOWLIST, checked before the RPC is sent. Not a denylist: a server that
   grows a tool in a later version does not silently gain reach. The check is
   on this side of the wire, so a server that misreports its own tool list
   cannot widen it.
2. resources and prompts are never called. The methods are not wired up at all,
   which is a stronger statement than passing `resources: false`.
3. Every call is audited -- server, tool, argument KEYS (never values, which
   carry repository names, message bodies and occasionally tokens), outcome.
4. Every result comes back FENCED. A README on a third-party server is text
   written by a stranger, exactly like a web page the research agent fetched,
   and it goes through the same `guard.wrap_untrusted` so it cannot close the
   block and start speaking as the system.

CONNECTION LIFETIME
-------------------
The SDK's client is asyncio and the agents are synchronous, so the session runs
on a private event loop in a daemon thread and sync callers hand work to it
with `run_coroutine_threadsafe`. One session per `with` block, not per call: a
stdio server here is a `docker run`, which costs a second or two to start, and
an agent's tool call typically makes several downstream calls in a loop.
"""

from __future__ import annotations

import asyncio
import json
import threading
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

from . import guard
from .errors import AgentError

# A single downstream reply is third-party text heading for a model's context.
# Cap it here as well as in the fence, so a server that returns a megabyte
# cannot cost a turn before wrap_untrusted ever sees it.
MAX_RESULT_CHARS = 20000
DEFAULT_TIMEOUT = 120.0


@dataclass
class StdioSpec:
    """A server started as a child process. `command` is not shell-interpreted."""
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] | None = None
    cwd: str | None = None


@dataclass
class HttpSpec:
    """A remote server over streamable HTTP.

    `headers` carries the bearer token. It is never logged: __repr__ is
    suppressed on the dataclass field rather than trusted to call sites.
    """
    url: str
    headers: dict[str, str] = field(default_factory=dict, repr=False)


class Downstream:
    """One policy-enforced connection to one third-party MCP server.

        with Downstream(name="github", spec=spec, allow={"get_file_contents"},
                        audit=boot.audit) as gh:
            gh.call("get_file_contents", {"owner": "...", "repo": "..."})
    """

    def __init__(self, *, name: str, spec: StdioSpec | HttpSpec,
                 allow: set[str], audit, timeout: float = DEFAULT_TIMEOUT):
        if not allow:
            # An empty allowlist is almost certainly a config mistake, and the
            # failure it produces ("tool not allowed") would be blamed on the
            # tool rather than on the empty set. Fail where the mistake is.
            raise ValueError(f"{name}: allow set must not be empty")
        self.name = name
        self.spec = spec
        self.allow = set(allow)
        self.audit = audit
        self.timeout = timeout
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._session = None
        self._stack: AsyncExitStack | None = None
        self._ready = threading.Event()
        self._error: BaseException | None = None

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> "Downstream":
        self._thread = threading.Thread(target=self._run_loop, daemon=True,
                                        name=f"mcp-{self.name}")
        self._thread.start()
        if not self._ready.wait(timeout=self.timeout):
            raise AgentError("mcp_unavailable",
                             f"the {self.name} MCP server did not start within "
                             f"{self.timeout:.0f}s")
        if self._error is not None:
            raise AgentError("mcp_unavailable",
                             f"could not connect to the {self.name} MCP server: "
                             f"{type(self._error).__name__}")
        return self

    def __exit__(self, *_exc: Any) -> None:
        loop = self._loop
        if loop is not None:
            # Closing the stack terminates the child process / HTTP session.
            asyncio.run_coroutine_threadsafe(self._teardown(), loop).result(30)
            loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=15)

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        loop.create_task(self._connect())
        loop.run_forever()
        loop.close()

    async def _connect(self) -> None:
        try:
            from mcp import ClientSession
            self._stack = AsyncExitStack()
            if isinstance(self.spec, StdioSpec):
                from mcp.client.stdio import StdioServerParameters, stdio_client
                params = StdioServerParameters(
                    command=self.spec.command, args=list(self.spec.args),
                    env=self.spec.env, cwd=self.spec.cwd)
                # The server's stderr is its diagnostics, not our protocol
                # stream; it must never reach OUR stdout, which is the JSON-RPC
                # channel Hermes is reading.
                import sys
                read, write = await self._stack.enter_async_context(
                    stdio_client(params, errlog=sys.stderr))
            else:
                # create_mcp_http_client rather than importing httpx directly:
                # the SDK vendors its HTTP client as `httpx2`, and naming the
                # module here is how the first version of this broke -- an
                # ImportError on `httpx` in the one transport that had no test
                # covering it, because GitHub and Context7 are both stdio.
                from mcp.client.streamable_http import (
                    create_mcp_http_client, streamable_http_client)
                client = await self._stack.enter_async_context(
                    create_mcp_http_client(headers=dict(self.spec.headers),
                                           timeout=self.timeout))
                streams = await self._stack.enter_async_context(
                    streamable_http_client(self.spec.url, http_client=client))
                read, write = streams[0], streams[1]

            session = await self._stack.enter_async_context(
                ClientSession(read, write, read_timeout_seconds=self.timeout))
            await session.initialize()
            self._session = session
        except BaseException as exc:      # noqa: BLE001 - reported, not raised here
            self._error = exc
        finally:
            self._ready.set()

    async def _teardown(self) -> None:
        if self._stack is not None:
            try:
                await self._stack.aclose()
            except BaseException:          # noqa: BLE001 - shutdown is best effort
                pass

    def _await(self, coro, timeout: float | None = None):
        if self._loop is None or self._session is None:
            raise AgentError("mcp_unavailable",
                             f"the {self.name} MCP server is not connected")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout or self.timeout)

    # -- policy ------------------------------------------------------------

    def available(self) -> list[str]:
        """Allowlisted tools the server actually offers, as (name, description).

        Intersected, not unioned: a tool in the allowlist that the server does
        not have is a stale allowlist, and a tool on the server that is not in
        the allowlist does not exist as far as this agent is concerned.
        """
        result = self._await(self._session.list_tools())
        offered = {t.name for t in (result.tools or []) if getattr(t, "name", None)}

        # A name in the allowlist that the server does not offer is a STALE
        # ALLOWLIST, and it used to fail silently: the tool was dropped, the
        # model called it anyway from memory, and the provider rejected the
        # turn with "attempted to call tool 'get-library-docs' which was not in
        # request.tools" -- a message that points at the model, not at the
        # config line that actually moved. Context7 renamed that tool between
        # major versions. Say so where the mistake is.
        stale = sorted(self.allow - offered)
        if stale:
            self.audit.write("mcp_allowlist_stale", server=self.name,
                             missing=stale, offered=len(offered))

        return [
            {"name": t.name, "description": (t.description or "")[:400],
             "schema": getattr(t, "inputSchema", None)}
            for t in (result.tools or []) if t.name in self.allow
        ]

    def call(self, tool: str, arguments: dict | None = None) -> str:
        """Call one allowlisted tool. Returns FENCED text, never raw.

        The allowlist check happens before the RPC, so a refused tool is never
        sent anywhere -- the third party does not even learn it was asked for.
        """
        arguments = dict(arguments or {})
        if tool not in self.allow:
            self.audit.write("mcp_refused", server=self.name, tool=tool,
                             reason="not in this agent's allowlist",
                             arg_keys=sorted(arguments))
            raise AgentError(
                "tool_not_allowed",
                f"'{tool}' is not on the {self.name} allowlist for this agent. "
                f"Allowed: {', '.join(sorted(self.allow))}.")

        # Argument KEYS only. Values carry repo names, branch names, message
        # bodies and, on a bad day, a token someone pasted into a field.
        self.audit.write("mcp_call", server=self.name, tool=tool,
                         arg_keys=sorted(arguments))
        try:
            result = self._await(self._session.call_tool(tool, arguments))
        except AgentError:
            raise
        except Exception as exc:
            self.audit.write("mcp_error", server=self.name, tool=tool,
                             error=type(exc).__name__)
            raise AgentError("mcp_call_failed",
                             f"the {self.name} server failed to run '{tool}' "
                             f"({type(exc).__name__})") from None

        text = _result_text(result)
        failed = bool(getattr(result, "isError", False))
        self.audit.write("mcp_result", server=self.name, tool=tool,
                         ok=not failed, chars=len(text))
        if failed:
            raise AgentError("mcp_tool_error",
                             f"{self.name}.{tool} returned an error: {text[:400]}")
        return guard.wrap_untrusted(text, label=f"{self.name}.{tool} result",
                                    source=self.name, limit=MAX_RESULT_CHARS)


def as_tools(available: list[dict]) -> list[dict]:
    """Downstream tool metadata -> OpenAI-style function definitions.

    The schema is passed through as the server published it. It is only ever
    used to help the model fill in arguments; it is NOT the thing that decides
    what may run. That is `Downstream.allow`, checked on this side of the wire,
    so a server that publishes a flattering schema gains nothing.
    """
    tools = []
    for entry in available:
        schema = entry.get("schema") or {"type": "object", "properties": {}}
        tools.append({"type": "function", "function": {
            "name": entry["name"],
            "description": entry.get("description") or "",
            "parameters": schema,
        }})
    return tools


def loop(boot, downstream: "Downstream", *, system: str, instruction: str,
         task: bool = False, once_only: set[str] | None = None,
         max_steps: int = 8, max_tokens: int = 1500) -> dict:
    """Let the model drive one downstream server until it answers.

    The same shape as the pm agent's ClickUp loop, for the same reason: the
    model proposes a call and this function disposes of it. Every dispatch goes
    through `Downstream.call`, so the allowlist is enforced even when the model
    has been talked into asking for something else -- which is the whole point,
    because the text it is reading comes from the server it is calling.
    """
    tools = as_tools(downstream.available())
    if not tools:
        raise AgentError("mcp_unavailable",
                         f"the {downstream.name} server offers none of the "
                         f"tools this agent is allowed to use")

    # `task=True` means the caller's instruction is the thing to DO, so it
    # is fenced but authoritative; otherwise it is material to answer about.
    # Getting this wrong is not a subtle failure: an action tool given the
    # read framing refuses every write it is asked for. See guard.py.
    wrap = guard.task_block if task else guard.instruction_block
    messages: list[dict] = [{"role": "user", "content": wrap(instruction)}]
    performed: list[dict] = []
    # Tools that may be called at most once per invocation, enforced HERE
    # rather than asked for in the prompt. `docs_query` spent eight of eight
    # steps re-resolving the same library id and never fetched the document;
    # telling it not to in words reduced that to five. A budget the model can
    # ignore is not a budget.
    once_only = set(once_only or ())
    spent: set[str] = set()

    for step in range(max_steps):
        reply = boot.llm.complete(system=system, user="", messages=messages,
                                  tools=tools, max_tokens=max_tokens,
                                  temperature=0.1)
        calls = reply.get("tool_calls") or []
        if not calls:
            return {"answer": (reply.get("text") or "").strip(),
                    "operations": performed, "steps": step + 1,
                    "model": reply.get("model")}

        messages.append({"role": "assistant", "content": reply.get("text") or "",
                         "tool_calls": calls})

        for call in calls:
            function = (call or {}).get("function") or {}
            name = function.get("name")
            try:
                args = json.loads(function.get("arguments") or "{}")
            except Exception:
                args = {}
            if name in once_only and name in spent:
                content = json.dumps({
                    "error": "already_called",
                    "detail": (f"'{name}' may be called once per request and you "
                               f"have already called it. Use the result you "
                               f"have and move on to the next step.")})
                performed.append({"tool": name, "args": args, "ok": False,
                                  "error": "already_called"})
                messages.append({"role": "tool", "tool_call_id": call.get("id"),
                                 "content": content})
                continue
            try:
                content = downstream.call(name, args)
                spent.add(name)
                performed.append({"tool": name, "args": args, "ok": True})
            except AgentError as exc:
                content = json.dumps({"error": exc.code, "detail": exc.detail})
                performed.append({"tool": name, "args": args, "ok": False,
                                  "error": exc.code})
            messages.append({"role": "tool", "tool_call_id": call.get("id"),
                             "content": content[:12000]})

    return {"answer": ("I could not finish within the step budget. What I did "
                       "is listed in `operations`."),
            "operations": performed, "steps": max_steps, "incomplete": True}


def _result_text(result: Any) -> str:
    """Flatten a CallToolResult to text.

    structuredContent first when present -- it is the typed answer -- then the
    text blocks. Non-text blocks (images, audio, embedded resources) are named
    rather than rendered: this agent has no use for their bytes and dropping
    them silently would make a result look empty rather than partial.
    """
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict) and structured:
        try:
            return json.dumps(structured, indent=1, default=str)[:MAX_RESULT_CHARS]
        except Exception:
            pass

    parts: list[str] = []
    for block in (getattr(result, "content", None) or []):
        kind = getattr(block, "type", None)
        if kind == "text":
            parts.append(str(getattr(block, "text", "")))
        else:
            parts.append(f"[{kind or 'unknown'} content omitted]")
    return ("\n".join(parts))[:MAX_RESULT_CHARS] or "[the server returned no content]"
