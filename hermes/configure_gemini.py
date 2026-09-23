#!/usr/bin/env python3
"""Hermes on Gemini, with OpenRouter behind it and Groq out of the chain.

WHY GEMINI AT ALL
-----------------
The constraint that decided the previous split has not moved: Hermes' Telegram
tool surface costs ~6,866 tokens per model call and a routing turn is at least
two calls, so ~13.7k tokens land inside one minute. Groq's ceiling is 8,000
tokens per minute, which is why Hermes cannot run there. OpenRouter's free tier
survives that but allows only 50 requests per DAY, account-wide.

Gemini is the third shape: its free tier meters *requests per minute*, not
tokens, and a per-minute limit recovers in seconds. Whether that is enough is
an empirical question, which is what the ``measure`` mode exists to answer.

WHAT SHIPS, AND WHAT GETS MEASURED -- AND WHY THEY DIFFER
---------------------------------------------------------
``gemini`` is a BUILT-IN provider name. Hermes resolves it to its own native
adapter (agent/gemini_native_adapter.py) and a ``providers.gemini.api`` entry
pointing somewhere else is ignored outright -- discovered the blunt way: a
measurement run configured that way produced an empty proxy log and a 429
carrying the native adapter's error format, so the traffic had gone straight to
Google.

Two facts then decide the shape of this script:

  - The native transport is chosen by HOSTNAME. ``is_native_gemini_base_url()``
    is true only for generativelanguage.googleapis.com or a Vertex express
    base, so a local proxy cannot sit in the native path at all: Hermes would
    keep speaking native REST to something speaking OpenAI.
  - A custom provider under any OTHER name gets the generic
    OpenAI-compatible transport, exactly as ``groq`` does, and honours ``api``.

So ``restore`` ships the built-in native ``gemini`` provider, which is the
first-class path (thought-signature handling for Gemini 3 thinking models,
``parametersJsonSchema`` tool encoding), and ``measure`` routes through a
custom ``gemini_proxy`` provider on the OpenAI-compatible surface, which is the
only surface a proxy can observe.

That difference is a real limitation of the measurement, not a detail to bury:
the measured numbers come from ``/v1beta/openai`` and the shipped path is
native REST. Token accounting and tool-schema encoding differ slightly between
them. What does NOT differ is the thing being measured -- request count per
turn, and the quota that meters it, which is enforced per project on the
``generate_content_free_tier_requests`` metric regardless of surface.

MODES
-----
    python hermes/configure_gemini.py measure   # gemini_proxy, NO fallback
    python hermes/configure_gemini.py restore   # native gemini, fallback back

``measure`` empties the fallback chain on purpose. With OpenRouter behind it,
Hermes reports the fallback's error and the real one never surfaces -- that is
exactly how a Groq 400 stayed hidden behind a 429 for a day. An empty chain is
the only way an error is attributable.
"""

from __future__ import annotations

import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

# Backups accumulate forever otherwise: 49 copies of config.yaml, every one
# holding a plaintext bot token, none covered by the approval patch. Pruned at
# the moment one is created, which is the only moment the count can grow.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from backup_prune import prune as _prune_backups  # noqa: E402


GEMINI_UPSTREAM = "https://generativelanguage.googleapis.com/v1beta/openai"
PROXY_BASE = "http://127.0.0.1:8799/v1"
# Any name but "gemini" -- see the module docstring.
PROXY_PROVIDER = "gemini_proxy"

# Default primary. gemini-3.7-flash is capped at 5 requests/minute on the free
# tier and a routing turn costs 4-5, so it cannot complete one turn without
# 429ing; gemini-3.5-flash-lite was measured at 10+ with no cap hit and emits
# tool_calls correctly. Override with --model.
HERMES_MODEL = "gemini-3.5-flash-lite"
# TWO fallback tiers, in order. The reason there are two is a quota shape the
# previous configuration had wrong.
#
# The README said Gemini's free tier meters requests per minute, which recovers
# in seconds. It meters BOTH, and the one that actually bit during the live
# end-to-end run is a DAILY token cap:
#
#   429 RESOURCE_EXHAUSTED: Quota exceeded for metric:
#   generativelanguage.googleapis.com/generate_content_free_tier_input_token_count,
#   limit: 250000, model: gemini-3.5-flash-lite
#
# At ~6.9k prompt tokens per call and 3-10 calls a turn, 250k/day is roughly a
# few dozen routing turns. A per-minute limit recovers while you wait; this one
# does not come back until the day rolls over, so a single OpenRouter tier
# behind it -- 50 requests per DAY, account-wide -- is not depth, it is a second
# thing that runs out on the same afternoon. That is exactly what happened: two
# of the five live test turns finished on OpenRouter.
#
# Tier 1 is therefore another Gemini model, because THE QUOTA IS PER MODEL --
# the 429 names the model inside the metric. A different model is a different
# 250k bucket on the same key, at no cost and with no new credential.
#
#   gemini-3.1-flash-lite: HTTP 200, tool_calls=1, 1M context, free tier.
#   gemini-2.5-flash-lite: HTTP 404, "no longer available to new users".
#
# Not `gemini-flash-lite-latest` and not a `-preview` suffix: an alias can move
# under a running gateway, which is the same reason the sandbox pins RTK.
#
# Tier 2 stays OpenRouter, as the last resort when the whole Gemini key is done
# for the day. nemotron-3-ultra-550b-a55b replaces nemotron-3.5-lightning, which
# was measured misreporting in production -- see documentation/E2E-RESULTS.md,
# where it claimed a program had been run that the agent's own log shows was
# never executed, and appended two sentences about compliance workflows to a
# report about a text file.
FALLBACK_CHAIN = [
    {"provider": "gemini", "model": "gemini-3.1-flash-lite"},
    {"provider": "openrouter", "model": "nvidia/nemotron-3-ultra-550b-a55b:free"},
]

# Gemini's reasoning vocabulary is low/medium/high (models.dev, and the
# catalogue Hermes itself caches). Hermes encodes "thinking off" for a custom
# OpenAI-compatible profile as top-level ``reasoning_effort: "none"``, which is
# not in that set -- the identical mismatch that made every Groq turn 400 until
# extra_body pinned it. Pinned here for the same reason, and merged last.
EXTRA_BODY = {"reasoning_effort": "medium"}


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

    model = HERMES_MODEL
    if "--model" in argv:
        model = argv[argv.index("--model") + 1]
    # measure normally empties the chain so errors are attributable. Once the
    # question is "how does this behave in production", the fallback belongs
    # back in -- the proxy log still records every Gemini 429 directly, so
    # nothing is hidden by keeping it.
    keep_fallback = "--with-fallback" in argv

    config_path = hermes_home() / "config.yaml"
    if not config_path.exists():
        print(f"FAIL: {config_path} not found")
        return 1

    backup = config_path.with_name(
        f"config.yaml.bak-gemini-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(config_path, backup)
    print(f"backup: {backup.name}")
    _prune_backups(config_path.parent if "config_path" in dir() else backup.parent,
                   "config.yaml.bak-*")

    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

    providers = config.setdefault("providers", {})
    # Never define a provider called "gemini": the name is taken by the
    # built-in native adapter, which silently wins and ignores ``api``.
    providers.pop("gemini", None)

    # ``providers.groq.extra_body.reasoning_effort`` was the workaround for
    # Hermes encoding "thinking off" as reasoning_effort: "none", which Groq
    # rejects with HTTP 400. It has been dead config since Groq left the chain,
    # and worse than dead: agent_init._custom_provider_extra_body_for_agent()
    # returns None unless the provider name is "custom"/"custom:<key>", so a
    # ``providers.<builtin>.extra_body`` entry is NEVER applied at all. Measured
    # on a forced OpenRouter turn:
    #
    #   Fallback nvidia/...: extra_body resolved: None
    #
    # A control that reads as present and is not is the exact failure this
    # repository already had once, so it goes rather than sitting there looking
    # load-bearing. What actually keeps "none" off the wire is
    # agent.reasoning_effort below, which is asserted, not assumed.
    if providers.pop("groq", None) is not None:
        print("  providers.groq removed (dead config: extra_body is ignored for "
              "built-in provider names, and groq is not in Hermes' chain)")

    if mode == "measure":
        provider_name = PROXY_PROVIDER
        providers[provider_name] = {
            "api": PROXY_BASE,
            "key_env": "GEMINI_API_KEY",
            "extra_body": dict(EXTRA_BODY),
        }
        print(f"  providers.{provider_name} -> {PROXY_BASE} "
              f"(key_env: GEMINI_API_KEY, extra_body.reasoning_effort=medium)")
    else:
        provider_name = "gemini"
        providers.pop(PROXY_PROVIDER, None)
        print(f"  providers.{PROXY_PROVIDER} removed; using the built-in "
              f"native gemini provider at {GEMINI_UPSTREAM.rsplit('/', 1)[0]}")

    chosen_model = model
    model_cfg = config.setdefault("model", {})
    previous = f"{model_cfg.get('provider')}/{model_cfg.get('default')}"
    model_cfg["default"] = chosen_model
    model_cfg["provider"] = provider_name
    model_cfg["api_mode"] = "chat_completions"
    # A stale base_url beside a custom provider is a way to send the wrong key
    # to the wrong host. The provider entry is the single source of the URL.
    model_cfg.pop("base_url", None)
    print(f"  model: {previous} -> {provider_name}/{chosen_model}")

    if mode == "measure" and not keep_fallback:
        config["fallback_providers"] = []
        print("  fallback_providers -> [] (empty, so errors surface)")
    else:
        config["fallback_providers"] = [dict(tier) for tier in FALLBACK_CHAIN]
        for index, tier in enumerate(FALLBACK_CHAIN, start=1):
            print(f"  fallback {index} -> {tier['provider']}/{tier['model']}")

    config_path.write_text(
        yaml.safe_dump(config, sort_keys=False, allow_unicode=True,
                       default_flow_style=False, width=100),
        encoding="utf-8",
    )

    # Re-read and verify rather than trusting the write.
    check = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    problems = []
    check_providers = check.get("providers") or {}
    if "gemini" in check_providers:
        problems.append("a provider named 'gemini' is defined; the built-in "
                        "adapter will ignore it and traffic will bypass any "
                        "configured base_url")
    if mode == "measure":
        entry = check_providers.get(PROXY_PROVIDER) or {}
        if entry.get("api") != PROXY_BASE:
            problems.append(f"providers.{PROXY_PROVIDER}.api not set")
        if entry.get("key_env") != "GEMINI_API_KEY":
            problems.append(f"providers.{PROXY_PROVIDER}.key_env not set")
        if (entry.get("extra_body") or {}).get("reasoning_effort") not in (
                "low", "medium", "high"):
            problems.append("extra_body.reasoning_effort missing or invalid")
    elif PROXY_PROVIDER in check_providers:
        problems.append(f"providers.{PROXY_PROVIDER} survived a restore")
    if (check.get("model") or {}).get("provider") != provider_name:
        problems.append(f"model.provider not {provider_name}")
    if (check.get("model") or {}).get("default") != chosen_model:
        problems.append("model.default not set")

    # The reasoning-effort trap. transports/chat_completions.py encodes
    # "thinking off" as effort "none", and "none" is not in the vocabulary Gemini
    # or Groq accept -- it is returned as HTTP 400. So the thing that has to stay
    # true is that reasoning is never OFF, and it is checked here rather than
    # assumed from a providers.* entry that is silently ignored (see above).
    effort = (check.get("agent") or {}).get("reasoning_effort")
    if effort not in ("low", "medium", "high"):
        problems.append(
            f"agent.reasoning_effort is {effort!r}; must be low/medium/high. "
            f"Anything falsy makes Hermes send reasoning effort \"none\", which "
            f"is rejected with HTTP 400.")

    fallbacks = check.get("fallback_providers")
    if mode == "measure" and not keep_fallback:
        if fallbacks:
            problems.append("fallback_providers not empty; errors will be masked")
    elif [dict(f) for f in (fallbacks or [])] != [dict(t) for t in FALLBACK_CHAIN]:
        # Exact, not "non-empty": depth is the point, and a chain that silently
        # collapsed to one tier looks fine to a truthiness check.
        problems.append(f"fallback chain is {fallbacks}, expected {FALLBACK_CHAIN}")

    # "No Groq in Hermes' chain" is an invariant, not a side effect: assert it
    # rather than assume the absence held through a YAML round-trip.
    chain = [(check.get("model") or {}).get("provider")]
    chain += [f.get("provider") for f in (fallbacks or [])]
    if "groq" in chain:
        problems.append(f"groq is back in Hermes' chain: {chain}")

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
    print(f"OK: re-parsed; gemini primary, chain={chain}, "
          "toolset and MCP invariants intact")
    return 0


if __name__ == "__main__":
    sys.exit(main())
