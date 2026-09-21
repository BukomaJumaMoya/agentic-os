#!/usr/bin/env python3
"""Hermes on OpenRouter, agents on Groq. Both limits measured, not guessed.

WHY THIS SPLIT
--------------
Neither free tier can run the whole system, but they fail in opposite ways, so
each provider goes where its limit does not bite.

OpenRouter free: 50 model requests per DAY, account-wide across every ``:free``
model, resetting at 00:00 UTC. That is fatal for agents (one research question
is several calls) but survivable for an orchestrator, whose turns are few and
deliberate. Context is 1M tokens, which matters below.

Groq free: 8,000 tokens per MINUTE on every tool-capable model, and a per-minute
limit recovers in seconds. Fine for agents, which send small payloads.

WHY HERMES CANNOT RUN ON GROQ
-----------------------------
Measured through a logging proxy on the real Telegram tool surface:

    system prompt            16,378 chars   ~4,095 tokens
    built-in tools            7,965 chars   ~1,991 tokens
    three MCP agents' tools   3,121 chars     ~780 tokens
    ------------------------------------------------------
    one model call           27,464 chars   ~6,866 tokens

A routing turn takes at least two calls -- one to choose the tool, one to report
its result -- so ~13,700 tokens land inside one minute against an 8,000 ceiling.
Groq answers with HTTP 413 (``Limit 8000, Requested 14424``), which Hermes
reports as "this conversation has grown too large": misleading, because context
was never the constraint. Trimming does not close a 1.7x gap; roughly 12,400
chars of that prompt is Hermes' own scaffolding that no setting removes.

The two Groq models with a high limit (groq/compound and groq/compound-mini,
70,000 TPM) answer tool calls with:

    HTTP 400: `tool calling` is not supported with this model

and an orchestrator that routes to MCP agents is nothing but tool calling.

WHAT IS CONFIGURED HERE
-----------------------
Hermes  -> OpenRouter, nemotron-3-ultra (free, 1M context). The large context is
           not a luxury: the tool surface alone is ~6.9k tokens per call.
Fallback-> a second large free OpenRouter model. This does NOT help when the
           daily cap is hit (that is account-wide), but it does help when the
           primary model alone is overloaded, which is a different failure.
Agents  -> Groq, configured in each agent's own .env, not here.

The Groq provider entry stays defined so the agents' provider is documented in
one place and so switching back is a one-line change. Its extra_body is
load-bearing -- see the comment on it.
"""

from __future__ import annotations

import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

GROQ_BASE_URL = "https://api.groq.com/openai/v1"

# Chosen from Groq's live /models on 2026-09-21 and verified to emit tool_calls
# against a real request before being written here. The catalogue is small; of
# the 131k-context models this is the strongest general tool-caller.
GROQ_MODEL = "openai/gpt-oss-120b"

# Fallback only. Free, 1M context, and confirmed present on OpenRouter.
HERMES_MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"
HERMES_FALLBACK_MODEL = "nvidia/nemotron-3.5-lightning:free"


def hermes_home() -> Path:
    home = os.environ.get("HERMES_HOME", "").strip()
    if home:
        return Path(home)
    local = os.environ.get("LOCALAPPDATA", "")
    return (Path(local) if local else Path.home() / "AppData" / "Local") / "hermes"


def main() -> int:
    try:
        import yaml
    except ImportError:
        print("FAIL: pyyaml is required")
        return 1

    config_path = hermes_home() / "config.yaml"
    if not config_path.exists():
        print(f"FAIL: {config_path} not found")
        return 1

    raw = config_path.read_text(encoding="utf-8")
    backup = config_path.with_name(
        f"config.yaml.bak-groq-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(config_path, backup)
    print(f"backup: {backup.name}")

    config = yaml.safe_load(raw) or {}

    providers = config.setdefault("providers", {})
    providers["groq"] = {
        "api": GROQ_BASE_URL,
        "key_env": "GROQ_API_KEY",
        # LOAD-BEARING. Without this every turn dies with:
        #   HTTP 400: `reasoning_effort` must be one of `low`, `medium`, `high`
        #
        # Hermes encodes "thinking off" for a custom OpenAI-compatible profile
        # as top-level `reasoning_effort: "none"` (see the install's
        # agent/auxiliary_reasoning_floor.py). Its own level vocabulary is
        # none/minimal/low/medium/high/xhigh/max/ultra; Groq accepts only three
        # of those. Because Hermes does not know a *custom* provider's model
        # supports reasoning, it picks the off encoding, and Groq rejects it.
        #
        # Neither `agent.reasoning_effort: medium` nor `--reasoning medium`
        # changes what is sent -- both were tested and both still 400. Pinning
        # it here in the provider's extra_body does, because extra_body is
        # merged into the request last.
        #
        # This also has to be diagnosed with the fallback chain EMPTY: with
        # OpenRouter behind it, Hermes reports the fallback's error (a 429) and
        # the real 400 never surfaces.
        "extra_body": {"reasoning_effort": "medium"},
    }
    print(f"  providers.groq -> {GROQ_BASE_URL} (key_env: GROQ_API_KEY, "
          f"extra_body.reasoning_effort=medium)")

    model = config.setdefault("model", {})
    previous = f"{model.get('provider')}/{model.get('default')}"
    model["default"] = HERMES_MODEL
    model["provider"] = "openrouter"
    model["api_mode"] = "chat_completions"
    # See the module docstring: a stale OpenRouter base_url next to a custom
    # provider is a way to send the wrong key to the wrong host.
    model.pop("base_url", None)
    print(f"  model: {previous} -> openrouter/{HERMES_MODEL}")

    config["fallback_providers"] = [
        {"provider": "openrouter", "model": HERMES_FALLBACK_MODEL},
    ]
    print(f"  fallback_providers -> openrouter/{HERMES_FALLBACK_MODEL}")

    config_path.write_text(
        yaml.safe_dump(config, sort_keys=False, allow_unicode=True,
                       default_flow_style=False, width=100),
        encoding="utf-8",
    )

    # Re-read and verify rather than trusting the write.
    check = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    problems = []
    if (check.get("providers") or {}).get("groq", {}).get("api") != GROQ_BASE_URL:
        problems.append("providers.groq.api not set")
    if (check.get("model") or {}).get("provider") != "openrouter":
        problems.append("model.provider not openrouter")
    if (check.get("model") or {}).get("default") != HERMES_MODEL:
        problems.append("model.default not set")
    if not check.get("fallback_providers"):
        problems.append("fallback_providers missing")
    groq_entry = (check.get("providers") or {}).get("groq", {})
    if (groq_entry.get("extra_body") or {}).get("reasoning_effort") not in (
            "low", "medium", "high"):
        problems.append("groq extra_body.reasoning_effort missing or invalid "
                        "(every turn will 400)")
    # The rebuild's own invariants must survive a full YAML round-trip.
    telegram = (check.get("platform_toolsets") or {}).get("telegram") or []
    for banned in ("terminal", "code_execution", "file", "computer_use", "browser"):
        if banned in telegram:
            problems.append(f"telegram regained {banned}")
    if not check.get("mcp_servers"):
        problems.append("mcp_servers lost in round-trip")
    if check.get("command_allowlist") != []:
        problems.append("command_allowlist is no longer empty")

    if problems:
        print("FAIL: " + "; ".join(problems))
        return 1
    print("OK: re-parsed; Groq primary, OpenRouter fallback, "
          "toolset and MCP invariants intact")
    return 0


if __name__ == "__main__":
    sys.exit(main())
