#!/usr/bin/env python3
"""Hermes on DeepSeek (paid), Gemini then OpenRouter behind it, Groq out.

WHY DEEPSEEK
------------
Every earlier primary was a free tier, and every one ran out in a way that took
the orchestrator down for the rest of the day:

  OpenRouter free   50 requests per DAY, account-wide.
  Gemini free       250,000 input tokens per DAY, per model -- ~19 turns.
  Groq free         8,000 tokens per MINUTE, and one routing turn is ~13.7k,
                    so Hermes cannot run there at all. Still true; Groq stays
                    out of this chain and is asserted out below.

DeepSeek is a paid key with credit on it. Its limit is the balance, which is a
number that can be read (GET /user/balance) rather than a 429 that arrives
mid-turn. The cost per turn is measured, not estimated -- see COST below.

MODEL
-----
Taken from GET https://api.deepseek.com/models on 2026-09-30, not from docs:

    deepseek-flash    name "DeepSeek-V4.1-Flash", 1M context
    deepseek-v4-pro   name "DeepSeek-V4-Pro",     1M context

deepseek-chat and deepseek-reasoner are gone from the list (retired
2026-07-24). The flash tier is the primary.

COST
----
Three routing turns on the Telegram surface, through tools/logging_proxy.py,
priced at deepseek-flash's published rates (USD per 1M tokens; peak is
01:00-04:00 and 06:00-10:00 UTC on weekdays, off-peak is half):

                      cache hit   cache miss   output
    off-peak            0.003        0.15       0.60
    peak                0.006        0.30       1.20

    turn                 calls  prompt tokens/call  latency/call  off-peak   peak
    research             2+1*   9,113 -> 9,963      1.3-1.4s      $0.0011   $0.0023
    n8n list             2+1*   9,118 -> 9,357      1.2s          $0.0010   $0.0019
    research + kola      3+1*   9,139 -> 11,123     1.4-2.3s      $0.0007   $0.0014

    * one session-title call, ~250 tokens, after a self-healed 400.

About a tenth of a US cent per turn, because ~90% of every call after the
first is a cache hit at 1/50th of the miss price. Interactive use is noise.

What is NOT noise is the cron load. The same run caught a client-engine cron
job on the proxy: 4 calls, 18k -> 43k prompt tokens, $0.0075 off-peak / $0.015
peak -- seven times a routing turn -- and client-coordinator (every 5 min) plus
reply-processor (every 10 min) is 432 runs a day. If each costs what the one
observed run did, that is ~$3.30-$6.50 a day, against interactive use of a few
cents. One sample; an idle poll may be cheaper. Measure before trusting it.

WHY A CUSTOM PROVIDER, AND WHY IT IS NOT CALLED "deepseek"
---------------------------------------------------------
``deepseek`` is a BUILT-IN provider name, and a built-in name has two traps that
this repository has already stepped in once each:

  - Its own adapter wins and ``providers.<builtin>.api`` is ignored, so a
    measurement through the logging proxy would silently bypass the proxy
    (the Gemini lesson -- an empty proxy log and a 429 straight from Google).
  - ``agent_init._custom_provider_extra_body_for_agent()`` returns None unless
    the provider is ``custom`` or ``custom:<key>``, so extra_body under a
    built-in name is never applied (the Groq lesson).

So the entry is ``providers.deepseek_api`` and Hermes selects it as
``custom:deepseek_api``: the generic OpenAI-compatible transport, which honours
both ``api`` and ``extra_body``.

REASONING EFFORT
----------------
Hermes encodes "thinking off" for a custom OpenAI-compatible provider as
top-level ``reasoning_effort: "none"``, and Groq rejected that with HTTP 400.
DeepSeek accepts it, so no extra_body is set -- the measurement is in the
comment beside FALLBACK_CHAIN, taken with the chain EMPTY so a 400 could not
hide behind a failover.

MODES
-----
    python hermes/configure_providers.py measure   # via logging proxy, NO fallback
    python hermes/configure_providers.py measure --with-fallback
    python hermes/configure_providers.py restore   # direct to DeepSeek, chain back
"""

from __future__ import annotations

import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from backup_prune import prune as _prune_backups  # noqa: E402


DEEPSEEK_BASE_URL = "https://api.deepseek.com"
PROXY_BASE = "http://127.0.0.1:8799/v1"
# Not "deepseek" -- see the module docstring.
PROVIDER_KEY = "deepseek_api"
PROVIDER = f"custom:{PROVIDER_KEY}"
HERMES_MODEL = "deepseek-flash"

# Tier 1 is the previous primary: free, its own 250k/day bucket, proven with
# real routing turns (documentation/DESIGN-NOTES.md). Tier 2 is the last resort,
# and nemotron-3-ultra rather than lightning, which misreported in production.
FALLBACK_CHAIN = [
    {"provider": "gemini", "model": "gemini-3.5-flash-lite"},
    {"provider": "openrouter", "model": "nvidia/nemotron-3-ultra-550b-a55b:free"},
]

# NO extra_body, on evidence. The Groq defect (Hermes sends reasoning_effort
# "none" to a custom provider; Groq 400s) was tested for here with the fallback
# chain EMPTY, through tools/logging_proxy.py, on 2026-09-30:
#
#   routing calls     reasoning_effort "high" (from agent.reasoning_effort) -> 200
#   title aux call    reasoning_effort "none"                               -> 200
#   direct probe      none/low/medium/high/max -> 200 each; "bogus" -> 422
#
# DeepSeek accepts every value Hermes can send, so there is nothing to pin, and
# a pinned value would only override "none" on the cheap auxiliary calls and
# make them reason. The one 400 in the run is not effort at all:
#
#   title aux call    response_format json_schema -> 400 "This response_format
#                     type is unavailable now"; Hermes retries without it -> 200
#
# One wasted ~0.5s call per new session, self-healed by Hermes. Left alone.


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

    argv = sys.argv[1:]
    mode = (argv[0] if argv else "").lower()
    if mode not in ("measure", "restore"):
        print(__doc__)
        return 2
    keep_fallback = mode == "restore" or "--with-fallback" in argv
    base_url = PROXY_BASE if mode == "measure" else DEEPSEEK_BASE_URL

    config_path = hermes_home() / "config.yaml"
    if not config_path.exists():
        print(f"FAIL: {config_path} not found")
        return 1

    backup = config_path.with_name(
        f"config.yaml.bak-deepseek-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(config_path, backup)
    print(f"backup: {backup.name}")
    _prune_backups(config_path.parent, "config.yaml.bak-*")

    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

    providers = config.setdefault("providers", {})
    # Leftovers of earlier configurations. A built-in name here is either
    # ignored or wins over what this script sets; neither is wanted.
    for stale in ("deepseek", "gemini", "gemini_proxy", "groq"):
        if providers.pop(stale, None) is not None:
            print(f"  providers.{stale} removed")
    providers[PROVIDER_KEY] = {
        "api": base_url,
        "key_env": "DEEPSEEK_API_KEY",
    }
    print(f"  providers.{PROVIDER_KEY} -> {base_url} (key_env: DEEPSEEK_API_KEY)")

    model_cfg = config.setdefault("model", {})
    previous = f"{model_cfg.get('provider')}/{model_cfg.get('default')}"
    model_cfg.pop("model", None)  # not a Hermes key; a hand edit left one here
    model_cfg.pop("base_url", None)  # the provider entry is the one source of the URL
    model_cfg["default"] = HERMES_MODEL
    model_cfg["provider"] = PROVIDER
    model_cfg["api_mode"] = "chat_completions"
    print(f"  model: {previous} -> {PROVIDER}/{HERMES_MODEL}")

    if keep_fallback:
        config["fallback_providers"] = [dict(t) for t in FALLBACK_CHAIN]
        for index, tier in enumerate(FALLBACK_CHAIN, start=1):
            print(f"  fallback {index} -> {tier['provider']}/{tier['model']}")
    else:
        config["fallback_providers"] = []
        print("  fallback_providers -> [] (empty, so errors surface)")

    config_path.write_text(
        yaml.safe_dump(config, sort_keys=False, allow_unicode=True,
                       default_flow_style=False, width=100),
        encoding="utf-8",
    )

    # Re-read and verify rather than trusting the write.
    check = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    problems = []
    entry = (check.get("providers") or {}).get(PROVIDER_KEY) or {}
    if entry.get("api") != base_url:
        problems.append(f"providers.{PROVIDER_KEY}.api not {base_url}")
    if entry.get("key_env") != "DEEPSEEK_API_KEY":
        problems.append(f"providers.{PROVIDER_KEY}.key_env not set")
    if "deepseek" in (check.get("providers") or {}):
        problems.append("a provider named 'deepseek' is defined; the built-in "
                        "adapter ignores api and extra_body")
    if (check.get("model") or {}).get("provider") != PROVIDER:
        problems.append(f"model.provider not {PROVIDER}")
    if (check.get("model") or {}).get("default") != HERMES_MODEL:
        problems.append("model.default not set")

    effort = (check.get("agent") or {}).get("reasoning_effort")
    if effort not in ("low", "medium", "high"):
        problems.append(
            f"agent.reasoning_effort is {effort!r}; anything falsy makes Hermes "
            f"send \"none\", which the fallback tiers reject with HTTP 400")

    fallbacks = check.get("fallback_providers")
    if keep_fallback:
        # Exact, not truthy: a chain that collapsed to one tier looks fine to
        # a truthiness check.
        if [dict(f) for f in (fallbacks or [])] != FALLBACK_CHAIN:
            problems.append(f"fallback chain is {fallbacks}, expected {FALLBACK_CHAIN}")
    elif fallbacks:
        problems.append("fallback_providers not empty; errors will be masked")

    chain = [(check.get("model") or {}).get("provider")]
    chain += [f.get("provider") for f in (fallbacks or [])]
    if "groq" in chain:
        problems.append(f"groq is in Hermes' chain: {chain}")

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
    print(f"OK: re-parsed; chain={chain}, toolset and MCP invariants intact")
    return 0


if __name__ == "__main__":
    sys.exit(main())
