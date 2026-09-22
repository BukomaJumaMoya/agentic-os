#!/usr/bin/env python3
"""The LLM surface `proposal.py` expects, backed by the agent's own client.

The ported `proposal.py` calls `_llm.complete_json(system, user, schema)` and
raises `_llm.LLMError`. Rather than edit ported logic that was written against
real drafts, this module presents that same surface on top of
`agents/_common/llm.py`, so the port stays a port.

There is no key handling here. The agent's `boot.llm` already holds the model
chain and this agent's own credentials; `configure()` is handed that client at
startup and nothing else in this package knows how a model is reached.
"""

from __future__ import annotations

import json
import re
from typing import Any

# The archived orchestrator's cap, kept: an enquiry longer than this is refused
# rather than truncated, because a truncated enquiry produces a confident
# proposal for work the client did not describe.
MAX_ENQUIRY_CHARS = 8000

_client: Any = None


class LLMError(RuntimeError):
    """Carries `kind` and `detail` because the ported code sets both."""

    def __init__(self, message: str, *, kind: str = "llm_error", detail: str = ""):
        super().__init__(message)
        self.kind = kind
        self.detail = detail


class ConfigError(RuntimeError):
    """No usable model configuration."""


def configure(client: Any) -> None:
    global _client
    _client = client


def _extract_json(text: str) -> dict:
    """Pull one JSON object out of a model reply.

    Models wrap JSON in prose and fences however they please. Taking the first
    balanced object is more reliable than asking again, and a failure here is
    reported as invalid_output rather than retried -- a retry costs a whole
    draft and usually returns the same shape.
    """
    body = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.+?)```", body, re.S)
    if fenced:
        body = fenced.group(1).strip()
    try:
        return json.loads(body)
    except Exception:
        pass
    start = body.find("{")
    if start == -1:
        raise LLMError("model reply contained no JSON object",
                       kind="invalid_output", detail=body[:400])
    depth, in_string, escaped = 0, False, False
    for index in range(start, len(body)):
        char = body[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(body[start:index + 1])
                except Exception as exc:
                    raise LLMError(f"model reply is not valid JSON: {exc}",
                                   kind="invalid_output",
                                   detail=body[start:start + 400]) from None
    raise LLMError("model reply has an unterminated JSON object",
                   kind="invalid_output", detail=body[start:start + 400])


def complete_json(system: str, user: str, schema: dict | None = None) -> dict:
    """{"data": <parsed object>} -- the shape proposal.py expects."""
    if _client is None:
        raise ConfigError("the docs agent has no model client configured")

    instruction = system
    if schema:
        # The schema is described rather than enforced: _common/llm.py speaks
        # chat completions across two providers and neither guarantees
        # structured output on the free tiers. proposal._validate() is the
        # actual gate and rejects a reply that does not fit, so a model that
        # ignores the shape fails loudly instead of producing a wrong document.
        instruction += ("\n\nReply with a single JSON object and nothing else, "
                        "matching this schema:\n"
                        + json.dumps(schema, indent=1)[:4000])

    reply = _client.complete(system=instruction, user=user,
                             max_tokens=2000, temperature=0.2)
    text = (reply or {}).get("text") or ""
    if not text.strip():
        raise LLMError("model returned an empty reply", kind="invalid_output")
    return {"data": _extract_json(text), "model": (reply or {}).get("model")}
