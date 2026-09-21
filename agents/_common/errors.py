#!/usr/bin/env python3
"""Structured returns, and the redactor that keeps secrets out of them.

WHY NO TRACEBACK EVER LEAVES AN AGENT
-------------------------------------
Hermes is the orchestrator, and whatever an agent returns lands in Hermes'
context and, from there, usually in a Telegram message. A Python traceback in
that position is three bad things at once:

  1. It prints filesystem paths, which map the machine for anyone reading.
  2. It prints local variables' repr in some frames -- and in an agent whose
     whole job is calling an authenticated API, the local variables include the
     key.
  3. It is unstructured, so Hermes' model reads it as prose and tries to
     interpret it, which is how a crash turns into a confident wrong answer.

So every tool is wrapped. Exceptions become a fixed shape: ok=False, a stable
machine-readable `error` code, and a human `detail` that has been through the
redactor. The full traceback goes to the agent's own audit log, on disk, where
it is useful and not in anyone's message.

THE REDACTOR
------------
Two layers, because each catches what the other misses:

  - Value-based: the agent's own loaded secrets are registered at startup and
    replaced wherever they appear. Exact, and catches a key echoed back inside
    an upstream API's error message -- ClickUp and OpenRouter both do this.
  - Pattern-based: things shaped like credentials (long opaque tokens, Bearer
    headers, pk_/sk_/xoxb_ prefixes). Catches secrets this agent never loaded,
    such as one that appears in a fetched web page.

Redaction is applied on the way OUT, at the boundary, not at each call site,
because a call site that forgets is exactly the one that leaks.
"""

from __future__ import annotations

import functools
import re
import traceback
from typing import Any, Callable

# Values registered by an agent at startup. Never read back out.
_SECRET_VALUES: list[str] = []

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Authorization headers, in any casing, including the token after them.
    (re.compile(r"(?i)\b(bearer|basic|token)\s+[A-Za-z0-9._\-~+/=]{12,}"), r"\1 <redacted>"),
    # Known vendor prefixes.
    (re.compile(r"\b(sk|pk|rk|xoxb|xoxp|ghp|gho|ghs|github_pat|tvly|pk_live|sk_live)[-_][A-Za-z0-9._\-]{10,}"),
     "<redacted-key>"),
    # OpenRouter / OpenAI style.
    (re.compile(r"\bsk-or-v1-[A-Za-z0-9]{16,}"), "<redacted-key>"),
    # ClickUp personal tokens.
    (re.compile(r"\bpk_\d+_[A-Za-z0-9]{16,}"), "<redacted-key>"),
    # Telegram bot tokens: digits:base64ish.
    (re.compile(r"\b\d{6,12}:[A-Za-z0-9_\-]{30,}"), "<redacted-key>"),
    # key=value / "key": "value" assignments of anything credential-named.
    (re.compile(r"(?i)\b([A-Za-z0-9_]*(?:api[_-]?key|token|secret|password|passwd)"
                r"[A-Za-z0-9_]*)(\"?\s*[:=]\s*\"?)([^\s\"',}]{8,})"), r"\1\2<redacted>"),
]


def register_secrets(values: list[str]) -> None:
    """Register this agent's own secret values for exact redaction."""
    for value in values:
        text = str(value or "")
        if len(text) >= 8 and text not in _SECRET_VALUES:
            _SECRET_VALUES.append(text)


def redact(text: Any) -> str:
    """Mask credentials in a string bound for the orchestrator."""
    out = str(text)
    for value in _SECRET_VALUES:
        if value in out:
            out = out.replace(value, "<redacted-key>")
    for pattern, replacement in _PATTERNS:
        out = pattern.sub(replacement, out)
    return out


class AgentError(Exception):
    """An expected failure with a stable code. Its message is shown to Hermes.

    Raise this for anything the caller could act on -- a missing key, a bad
    argument, an upstream refusal. Anything else that escapes is a bug, and is
    reported as `internal_error` with no detail beyond the exception type.
    """

    def __init__(self, code: str, detail: str, *, retryable: bool = False,
                 extra: dict[str, Any] | None = None):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.retryable = retryable
        self.extra = extra or {}


def ok(**payload: Any) -> dict[str, Any]:
    return {"ok": True, **payload}


def fail(code: str, detail: str, *, retryable: bool = False,
         **extra: Any) -> dict[str, Any]:
    return {
        "ok": False,
        "error": code,
        "detail": redact(detail),
        "retryable": retryable,
        **extra,
    }


def guarded(audit=None) -> Callable:
    """Wrap a tool so no exception and no secret can escape it.

    `audit` is the agent's audit logger; the full traceback goes there and
    nowhere else.
    """

    def decorate(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
            try:
                result = func(*args, **kwargs)
            except AgentError as exc:
                if audit:
                    audit.write("tool_error", tool=func.__name__, code=exc.code,
                                detail=exc.detail)
                return fail(exc.code, exc.detail, retryable=exc.retryable, **exc.extra)
            except Exception as exc:  # noqa: BLE001 -- the point is to catch everything
                if audit:
                    audit.write("tool_crash", tool=func.__name__,
                                exception=type(exc).__name__,
                                traceback=traceback.format_exc())
                # Type name only. The message may quote an argument, and an
                # argument may be a key.
                return fail(
                    "internal_error",
                    f"{func.__name__} failed with {type(exc).__name__}. "
                    f"The details are in this agent's audit log; they are not "
                    f"returned because they can contain credentials.",
                )
            if isinstance(result, dict):
                return result
            return ok(result=result)

        return wrapper

    return decorate
