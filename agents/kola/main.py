#!/usr/bin/env python3
"""Kolaborate agent -- the only place `kola_call`'s operation name can be policed.

WHY THIS AGENT HAS TO EXIST
---------------------------
Kolaborate's MCP server publishes exactly TWO tools:

    kola_discover   search its catalogue
    kola_call       "Execute any Kola tool by exact name"

`kola_call` is a generic dispatcher. The operation it performs is named in its
ARGUMENTS, not in the tool name. That defeats every tool-level control there is:

  - Hermes' allowlist works on tool NAMES, so it could only allow `kola_call`
    entirely or ban it entirely. "Jobs read but not jobs write" is inexpressible.
  - Hermes' write-approval gate keys on a per-TOOL readOnlyHint. `kola_call`
    has none, so every call prompts, including reads -- and the prompt cannot
    say which of the catalogue's operations is about to run.

An agent can enforce what a tool-name allowlist cannot: an allowlist on the
INNER operation, read from the arguments, checked before the RPC is sent. That
is the whole reason this file exists, and it is why the earlier token-cost
argument for placing it behind an agent was withdrawn -- two tools cost almost
nothing. This is the real reason.

THE POLICY
----------
ALLOWED_OPERATIONS is a literal set of operation names. Anything else is
refused here, on our side of the wire, before Kolaborate learns it was asked.
`kola_discover` is read-only and unrestricted, so the catalogue can be browsed
without granting the ability to act on it.

Split read/write like every other agent: `kola_query` gets the read operations,
`kola_action` gets read plus write and is approval-gated by Hermes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from _common import guard                      # noqa: E402
from _common import mcp_client                 # noqa: E402
from _common.errors import AgentError, ok      # noqa: E402
from _common.server import bootstrap, fatal    # noqa: E402

AGENT = "kola"
VERSION = "1.0.0"

DEFAULT_URL = "https://mcp.kolaborate.africa/api/mcp"

# The two tools the server actually publishes.
TOOL_DISCOVER = "kola_discover"
TOOL_CALL = "kola_call"

# The INNER operations this agent may dispatch through kola_call. Empty on
# purpose for writes until a real one is needed: an allowlist that starts
# permissive never gets narrowed.
#
# Names come from the live catalogue via kola_discover. They are listed rather
# than pattern-matched, because "anything starting with get_" is the kind of
# rule that ages into "get_or_create_".
# Taken from the live catalogue (`kola_catalogue(category="jobs")`), not guessed.
# The first version of this list WAS guessed -- listJobs, getJob, searchJobs --
# and every one of them was rejected by the server, because the real names are
# snake_case and category-prefixed. A guessed allowlist fails safe (nothing
# runs) but it also fails silently useless.
ALLOWED_READ_OPERATIONS = {
    "jobs_list",            # browse open jobs
    "jobs_get",             # one job
    "jobs_applicants_list",  # who applied to my job
    "jobs_get_client_notes",
}

# Empty on purpose. The jobs category alone offers jobs_create, jobs_set_slug,
# jobs_update_company, jobs_update_skills, jobs_update_banner,
# jobs_rollback_applied_notes and jobs_retrigger_suggestions -- all writes
# against a live marketplace. None is needed yet, and an allowlist that starts
# permissive never gets narrowed. Adding one is an edit here plus a readOnlyHint
# decision on the tool that dispatches it.
ALLOWED_WRITE_OPERATIONS: set[str] = set()

INSTRUCTIONS = """Kolaborate marketplace, read-only for now.

kola_catalogue browses what the account can reach. kola_query runs one of a
small set of allowed READ operations.

The upstream server exposes a single generic `kola_call` that can run any of its
operations; this agent refuses every operation that is not on its own list, so
the catalogue being large does not make the reach large."""


def spec(boot) -> mcp_client.HttpSpec:
    key = (boot.config.get("KOLA_API_KEY") or "").strip()
    if not key:
        raise AgentError("not_configured",
                         "KOLA_API_KEY is not set in agents/kola/.env")
    url = (boot.config.get("KOLA_MCP_URL") or DEFAULT_URL).strip()
    return mcp_client.HttpSpec(url=url, headers={"Authorization": f"Bearer {key}"})


def operation_of(arguments: dict) -> str:
    """The inner operation name, whatever kola_call happens to call the field."""
    for field in ("name", "tool", "operation", "toolName"):
        value = (arguments or {}).get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def main() -> None:
    try:
        boot = bootstrap(
            agent=AGENT, version=VERSION, instructions=INSTRUCTIONS,
            required=["KOLA_API_KEY"],
            optional=["KOLA_MCP_URL", "GROQ_API_KEY", "OPENROUTER_API_KEY"],
            default_model="openai/gpt-oss-120b",
        )
    except Exception as exc:
        fatal(AGENT, exc)
        return

    def dispatch(operation: str, arguments: dict, allowed: set[str]) -> dict:
        """Run ONE inner operation, refusing anything off the list."""
        if operation not in allowed:
            boot.audit.write("kola_operation_refused", operation=operation,
                             allowed_count=len(allowed))
            raise AgentError(
                "operation_not_allowed",
                f"'{operation}' is not an operation this agent may run. "
                f"Allowed: {', '.join(sorted(allowed)) or '(none)'}. "
                f"Kolaborate's catalogue is larger than this list on purpose.")
        with mcp_client.Downstream(name="kola", spec=spec(boot),
                                   allow={TOOL_CALL, TOOL_DISCOVER},
                                   audit=boot.audit) as kola:
            boot.audit.write("kola_operation", operation=operation,
                             arg_keys=sorted(arguments or {}))
            return {"operation": operation,
                    "result": kola.call(TOOL_CALL,
                                        {"name": operation, "arguments": arguments or {}})}

    @boot.tool(
        name="kola_catalogue",
        description=("Browse the Kolaborate tool catalogue this account can "
                     "reach. READ ONLY: it lists what exists and runs nothing."),
        annotations={"readOnlyHint": True},
    )
    def kola_catalogue(query: str = "", category: str = "") -> dict:
        arguments = {k: v for k, v in (("query", query), ("category", category)) if v}
        with mcp_client.Downstream(name="kola", spec=spec(boot),
                                   allow={TOOL_DISCOVER, TOOL_CALL},
                                   audit=boot.audit) as kola:
            body = kola.call(TOOL_DISCOVER, arguments)
        return ok(catalogue=body,
                  allowed_read_operations=sorted(ALLOWED_READ_OPERATIONS),
                  allowed_write_operations=sorted(ALLOWED_WRITE_OPERATIONS),
                  note=("Listing an operation here does not mean this agent may "
                        "run it. Only the allowed_* lists can be dispatched."),
                  agent=AGENT, version=VERSION)

    @boot.tool(
        name="kola_query",
        description=("Run one allowed READ operation on the Kolaborate "
                     "marketplace and return its result. Cannot create, "
                     "update, apply or message."),
        annotations={"readOnlyHint": True},
    )
    def kola_query(operation: str, arguments: dict | None = None) -> dict:
        operation = str(operation or "").strip()
        if not operation:
            raise AgentError("bad_input", "operation must not be empty")
        out = dispatch(operation, arguments or {}, ALLOWED_READ_OPERATIONS)
        return ok(read_only=True, **out, agent=AGENT, version=VERSION)

    boot.run()


if __name__ == "__main__":
    main()
