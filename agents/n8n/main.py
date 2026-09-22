#!/usr/bin/env python3
"""n8n agent -- Hermes can trigger NAMED workflows, and nothing else.

WHAT HERMES CAN REACH THROUGH THIS
----------------------------------
Exactly the workflows listed in ALLOWED_WORKFLOWS, by a short name this file
chooses, invoked through their webhook path. Hermes never sees an n8n workflow
id, never sees the webhook path, and cannot name a workflow that is not in the
list -- the mapping is here, on this side of the wire.

THE ADMIN API IS NOT REACHABLE FROM HERE
----------------------------------------
`N8N_API_KEY` can create, edit, activate and delete any workflow, read every
credential reference and change n8n's own settings. That key is NOT loaded by
this agent: it lives in agents/n8n/.env for the operator's own scripts, and
`bootstrap()` is not given it, so it is not in `boot.config` and there is no
code path here that could send it.

Beyond that, `_webhook_url()` refuses any path that is not under /webhook/, so
even a mistaken entry in ALLOWED_WORKFLOWS cannot address /api/v1/... . Two
independent reasons the admin API is unreachable, because one of them is a
list someone could edit.

WHY THE WHOLE TOOL IS APPROVAL-GATED
------------------------------------
`run_workflow` carries no readOnlyHint, so on a `trust: untrusted` server every
call needs Bukoma's approval before the RPC is sent. That is deliberate even
for a workflow that only reads: what an n8n workflow does is defined inside
n8n, can be edited there, and is not visible from this side. A tool whose
effect is editable elsewhere is a write tool.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from _common import guard                      # noqa: E402
from _common.errors import AgentError, ok      # noqa: E402
from _common.server import bootstrap, fatal    # noqa: E402

AGENT = "n8n"
VERSION = "1.0.0"

BASE = "http://127.0.0.1:5678"
TIMEOUT = 60

# short name -> webhook path segment. Adding a workflow is an edit HERE, which
# is the point: n8n's own workflow list is not an allowlist.
ALLOWED_WORKFLOWS = {
    "clickup-task-assigned": "clickup-task-assigned-to-me",
}

INSTRUCTIONS = """Trigger a named, pre-approved n8n workflow.

list_workflows names what can be run. run_workflow runs one of them and returns
what it replied. Nothing else in n8n is reachable: no admin API, no workflow
editing, no credentials, and no workflow that is not on the list."""


def _webhook_url(path: str) -> str:
    """Build the webhook URL, refusing anything that is not a webhook.

    The check is on the CONSTRUCTED url, not on the caller's input, so a path
    containing '..' or a full URL cannot steer it elsewhere.
    """
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,120}", path):
        raise AgentError("bad_workflow",
                         "workflow paths are plain names; this one is not")
    url = f"{BASE}/webhook/{urllib.parse.quote(path, safe='')}"
    parsed = urllib.parse.urlparse(url)
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
            or parsed.port != 5678 or not parsed.path.startswith("/webhook/")):
        raise AgentError("bad_workflow", "refusing a non-webhook n8n URL")
    return url


def main() -> None:
    try:
        boot = bootstrap(
            agent=AGENT,
            version=VERSION,
            instructions=INSTRUCTIONS,
            required=[],
            # N8N_API_KEY is deliberately absent. _common/env.py only exposes
            # declared names, so the admin key is not in this process even
            # though it sits in the same .env file.
            optional=["GROQ_API_KEY", "OPENROUTER_API_KEY"],
            needs_llm=False,
        )
        if "N8N_API_KEY" in boot.config:
            raise RuntimeError("the n8n admin API key reached this agent's "
                               "config; it must not be declared")
    except Exception as exc:
        fatal(AGENT, exc)
        return

    @boot.tool(
        name="list_workflows",
        description=("Name the n8n workflows that may be run. Read-only: it "
                     "lists this agent's own allowlist and contacts nothing."),
        annotations={"readOnlyHint": True},
    )
    def list_workflows() -> dict:
        return ok(workflows=sorted(ALLOWED_WORKFLOWS),
                  detail=("These are the only workflows that can be run. n8n's "
                          "own workflow list is not reachable from here."),
                  agent=AGENT, version=VERSION)

    @boot.tool(
        name="run_workflow",
        description=("Run one named n8n workflow and return its reply. Only "
                     "the workflows from list_workflows can be run. Ask the "
                     "user to confirm before calling this."),
    )
    def run_workflow(workflow: str, payload: dict | None = None) -> dict:
        name = str(workflow or "").strip()
        if name not in ALLOWED_WORKFLOWS:
            boot.audit.write("workflow_refused", workflow=name,
                             reason="not in this agent's allowlist")
            raise AgentError(
                "workflow_not_allowed",
                f"'{name}' is not a workflow this agent may run. Allowed: "
                f"{', '.join(sorted(ALLOWED_WORKFLOWS))}.")

        body = json.dumps(payload or {}).encode("utf-8")
        url = _webhook_url(ALLOWED_WORKFLOWS[name])
        boot.audit.write("workflow_run", workflow=name,
                         payload_keys=sorted((payload or {}).keys()))

        request = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "User-Agent": f"juma-{AGENT}/{VERSION}"})
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                status = getattr(response, "status", 200)
                text = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            boot.audit.write("workflow_error", workflow=name, status=exc.code)
            raise AgentError("workflow_failed",
                             f"n8n returned HTTP {exc.code} for '{name}'",
                             retryable=exc.code >= 500) from None
        except Exception as exc:
            boot.audit.write("workflow_error", workflow=name,
                             error=type(exc).__name__)
            raise AgentError("workflow_unreachable",
                             f"could not reach n8n ({type(exc).__name__}). It "
                             f"listens on 127.0.0.1:5678 only.") from None

        boot.audit.write("workflow_done", workflow=name, status=status,
                         chars=len(text))
        # n8n's reply is composed inside n8n, by nodes that touch third-party
        # services. It is data, like any other fetched text.
        return ok(workflow=name, status=status,
                  result=guard.wrap_untrusted(text, label=f"n8n {name} reply",
                                              source="n8n", limit=8000),
                  agent=AGENT, version=VERSION)

    boot.run()


if __name__ == "__main__":
    main()
