#!/usr/bin/env python3
"""Is every credential in every .env actually valid? One harmless call each.

WHAT THIS IS FOR
----------------
A dead key does not announce itself. It surfaces as an agent that "sometimes
does not work", usually days later, usually as a fallback quietly carrying
traffic the primary was supposed to handle -- which is exactly what the live
end-to-end run found: two of five turns finished on the fallback and nothing
said the primary had hit a wall.

So: one authenticated request per key, to the cheapest read-only endpoint each
provider has, and a verdict per key NAME.

    python hermes/check_keys.py

WHAT IT NEVER PRINTS
--------------------
The value. Not a prefix, not a suffix, not four characters "for identification".
Output is the key name, which file it came from, its length, and valid/invalid.
Lengths are printed because a key that is 0 or 12 characters long is a
truncated paste, and that is a different problem from a rejected key.

Where a name has no validator, it says so rather than guessing. An unchecked
key reported as "unknown" is honest; reported as "ok" it is a lie that will be
believed for months.

DUPLICATES
----------
The same name appears in several .env files by design -- each agent holds its
own copy, because per-agent .env isolation is one of the invariants and sharing
one file would defeat it. The copies are compared BY DIGEST, so a drift ("pm
has a newer OpenRouter key than research") is reported without any value
leaving the process.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TIMEOUT = 45


def hermes_home() -> Path:
    home = os.environ.get("HERMES_HOME", "").strip()
    if home:
        return Path(home)
    local = os.environ.get("LOCALAPPDATA", "")
    return (Path(local) if local else Path.home() / "AppData" / "Local") / "hermes"


def env_files() -> list[tuple[str, Path]]:
    return [
        ("agents/research", REPO / "agents" / "research" / ".env"),
        ("agents/pm", REPO / "agents" / "pm" / ".env"),
        ("agents/coding", REPO / "agents" / "coding" / ".env"),
        ("hermes", hermes_home() / ".env"),
    ]


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.lstrip().startswith("#"):
            continue
        match = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$", line)
        if match and match.group(2).strip():
            values[match.group(1)] = match.group(2).strip().strip('"').strip("'")
    return values


# --------------------------------------------------------------------------
# one read-only call per provider
# --------------------------------------------------------------------------

# Groq sits behind Cloudflare, which rejects urllib's default User-Agent with
# "HTTP 403 error code: 1010" -- an edge block, not an auth failure. Without a
# UA this check reported a WORKING key as INVALID on all four .env files while
# the agents were using it successfully in the same minute. A credential
# checker that produces false negatives sends people to rotate live keys, so
# every request goes out with a real one.
USER_AGENT = "juma-agentic-os-keycheck/1.0 (+hermes/check_keys.py)"


def _get(url: str, headers: dict, data: bytes | None = None) -> tuple[int, str]:
    headers = {"User-Agent": USER_AGENT, **headers}
    request = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.status, ""
    except urllib.error.HTTPError as exc:
        # The body is never returned to the caller: some providers echo the
        # submitted credential back inside their error payload.
        return exc.code, ""
    except Exception as exc:
        return 0, type(exc).__name__


def check_groq(key: str):
    return _get("https://api.groq.com/openai/v1/models",
                {"Authorization": f"Bearer {key}"})


def check_openrouter(key: str):
    return _get("https://openrouter.ai/api/v1/key",
                {"Authorization": f"Bearer {key}"})


def check_gemini(key: str):
    return _get("https://generativelanguage.googleapis.com/v1beta/models",
                {"x-goog-api-key": key})


def check_clickup(key: str):
    return _get("https://api.clickup.com/api/v2/user", {"Authorization": key})


def check_telegram(key: str):
    return _get(f"https://api.telegram.org/bot{key}/getMe", {})


def check_tavily(key: str):
    """Tavily moved from an api_key field to bearer auth and still accepts
    both, so a 401 on one spelling is retried with the other before a key is
    called dead. max_results=1 keeps the call as small as the API allows."""
    body = json.dumps({"query": "ping", "max_results": 1}).encode()
    status, note = _get("https://api.tavily.com/search", {
        "Authorization": f"Bearer {key}", "Content-Type": "application/json"}, body)
    if status in (401, 403):
        legacy = json.dumps({"api_key": key, "query": "ping", "max_results": 1}).encode()
        status, note = _get("https://api.tavily.com/search",
                            {"Content-Type": "application/json"}, legacy)
    return status, note


VALIDATORS = {
    "GROQ_API_KEY": check_groq,
    "OPENROUTER_API_KEY": check_openrouter,
    "GEMINI_API_KEY": check_gemini,
    "CLICKUP_TOKEN": check_clickup,
    "TELEGRAM_BOT_TOKEN": check_telegram,
    "TAVILY_API_KEY": check_tavily,
}

# Settings, not credentials. Named explicitly rather than guessed from the name,
# because "looks like a config value" is how a real key ends up unchecked.
NOT_CREDENTIALS = {
    "FALLBACK_MODELS", "RESEARCH_MODEL", "RESEARCH_OPENROUTER_MODEL",
    "PM_MODEL", "PM_OPENROUTER_MODEL", "CLICKUP_TEAM_ID",
    "CODING_MODEL", "CODING_ROOT", "CODING_SANDBOX", "CODING_TOKEN_TOOLS",
    "TELEGRAM_ALLOWED_USERS", "TERMINAL_ENV", "TERMINAL_TIMEOUT",
    "TERMINAL_LIFETIME_SECONDS", "TERMINAL_MODAL_IMAGE",
    "BROWSER_INACTIVITY_TIMEOUT", "BROWSER_SESSION_TIMEOUT",
    "BROWSERBASE_ADVANCED_STEALTH", "BROWSERBASE_PROXIES",
    "IMAGE_TOOLS_DEBUG", "MOA_TOOLS_DEBUG", "VISION_TOOLS_DEBUG",
    "WEB_TOOLS_DEBUG",
}


def verdict(status: int, note: str) -> tuple[str, str]:
    if status == 200:
        return "valid", ""
    if status in (401, 403):
        return "INVALID", f"rejected (HTTP {status})"
    if status == 429:
        # The credential authenticated; the account is out of quota. Calling
        # that "invalid" would send someone to rotate a working key.
        return "valid", "authenticated but rate-limited (HTTP 429)"
    if status == 0:
        return "unknown", f"could not reach the provider ({note})"
    return "unknown", f"unexpected HTTP {status}"


def main() -> int:
    seen: dict[str, dict[str, str]] = {}
    rows: list[tuple[str, str, int, str, str]] = []

    sources = env_files()
    extra = os.environ.get("GEMINI_API_KEY", "").strip()

    for label, path in sources:
        if not path.exists():
            print(f"  (no .env at {path})")
            continue
        for name, value in sorted(parse_env(path).items()):
            seen.setdefault(name, {})[label] = hashlib.sha256(
                value.encode()).hexdigest()[:12]
            if name in NOT_CREDENTIALS:
                rows.append((label, name, len(value), "-", "not a credential"))
                continue
            validator = VALIDATORS.get(name)
            if validator is None:
                rows.append((label, name, len(value), "unknown", "no validator"))
                continue
            state, note = verdict(*validator(value))
            rows.append((label, name, len(value), state, note))

    if extra:
        # Hermes' primary provider key is a user environment variable, not a
        # .env line -- the README says so -- so it would otherwise go unchecked.
        seen.setdefault("GEMINI_API_KEY", {})["environment"] = hashlib.sha256(
            extra.encode()).hexdigest()[:12]
        state, note = verdict(*check_gemini(extra))
        rows.append(("environment", "GEMINI_API_KEY", len(extra), state, note))
    else:
        rows.append(("environment", "GEMINI_API_KEY", 0, "INVALID",
                     "not set; Hermes' primary provider has no key"))

    width = max(len(name) for _, name, *_ in rows)
    print(f"{'source':16} {'key':{width}} {'len':>4}  verdict")
    print("-" * (16 + width + 24))
    for label, name, length, state, note in rows:
        suffix = f"  ({note})" if note else ""
        print(f"{label:16} {name:{width}} {length:>4}  {state}{suffix}")

    print()
    for name, copies in sorted(seen.items()):
        digests = set(copies.values())
        if len(copies) > 1 and len(digests) > 1:
            print(f"  NOTE  {name} differs between {', '.join(sorted(copies))} "
                  f"({len(digests)} distinct values)")

    bad = [f"{label}:{name}" for label, name, _, state, _ in rows if state == "INVALID"]
    unknown = [f"{label}:{name}" for label, name, _, state, _ in rows if state == "unknown"]
    if unknown:
        print(f"\n{len(unknown)} key(s) not checked: {', '.join(unknown)}")
    if bad:
        print(f"\nFAIL: {len(bad)} invalid credential(s): {', '.join(bad)}")
        return 1
    print("\nOK: every credential with a validator authenticated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
