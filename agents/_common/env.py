#!/usr/bin/env python3
"""Per-agent environment loading.

ONE RULE, AND IT IS STRUCTURAL
------------------------------
An agent reads agents/<name>/.env and nothing else. Not the repository .env,
not Hermes' .env, not whatever the orchestrator happened to have exported when
it spawned this process.

The last of those is the one that needs enforcing in code rather than in a
comment. Hermes spawns each agent as a stdio subprocess, and a subprocess
inherits its parent's environment by default. "Hermes passes no secrets to
agents" is therefore not true merely because Hermes does not mean to: if
Hermes' own process holds OPENROUTER_API_KEY and CLICKUP_TOKEN, every agent it
spawns starts life holding them too.

So load() scrubs first and loads second. Anything in the inherited environment
that looks like a credential is deleted from os.environ before this agent's own
.env is read, unless this agent declared that name. The research agent cannot
reach a ClickUp token by accident because, by the time its tools run, the
variable does not exist in its process.

Scrubbing is by name pattern, not by value, because a value-based scrub would
need to know the secrets in order to remove them.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Names that hold a credential often enough that inheriting one is a mistake
# worth making impossible. Matched case-insensitively against the whole name.
_SECRET_NAME_RE = re.compile(
    r"(?:^|_)(?:API_?KEY|APIKEY|TOKEN|SECRET|PASSWORD|PASSWD|PWD|CREDENTIAL|"
    r"PRIVATE_?KEY|ACCESS_?KEY|CLIENT_?SECRET|SESSION_?KEY|AUTH)(?:$|_)",
    re.IGNORECASE,
)

# Specific names worth naming, because they are the ones actually on this
# machine and a pattern miss would be silent.
_KNOWN_SECRET_NAMES = {
    "OPENROUTER_API_KEY",
    "TAVILY_API_KEY",
    "CLICKUP_TOKEN",
    "CLICKUP_TEAM_ID",
    "GEMINI_API_KEY",
    "GROQ_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_ALLOWED_USERS",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "ZAI_API_KEY",
    "KIMI_API_KEY",
    "MINIMAX_API_KEY",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SESSION_TOKEN",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "NPM_TOKEN",
    "HERMES_API_KEY",
}


class EnvError(RuntimeError):
    """The agent cannot start: a declared key is missing or the file is bad."""


def env_path(agent: str) -> Path:
    return REPO_ROOT / "agents" / agent / ".env"


def looks_secret(name: str) -> bool:
    return name.upper() in _KNOWN_SECRET_NAMES or bool(_SECRET_NAME_RE.search(name))


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE. No interpolation, no `export`, no multi-line values.

    Deliberately strict rather than clever: a .env parser that resolves $VAR
    lets a value in one file reach a value in another, which is the property
    this module exists to remove.
    """
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for lineno, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            raise EnvError(f"{path.name} line {lineno}: not a KEY=VALUE assignment")
        name, _, value = line.partition("=")
        name = name.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise EnvError(f"{path.name} line {lineno}: invalid variable name")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        out[name] = value
    return out


def scrub_inherited(keep: set[str]) -> list[str]:
    """Delete inherited credential-shaped variables. Returns the names removed.

    Names only ever leave this function; values are never returned, logged or
    reported.
    """
    removed = []
    for name in list(os.environ):
        if name in keep:
            continue
        if looks_secret(name):
            del os.environ[name]
            removed.append(name)
    return sorted(removed)


def load(agent: str, required: list[str] | None = None,
         optional: list[str] | None = None) -> dict[str, str]:
    """Scrub the inherited environment, then load this agent's own .env.

    Returns the agent's own values. The same values are placed in os.environ so
    that libraries which read the environment directly still work, but the
    returned mapping is what agent code should use.
    """
    required = list(required or [])
    optional = list(optional or [])
    declared = set(required) | set(optional)

    removed = scrub_inherited(keep=set())
    values = parse_env_file(env_path(agent))

    missing = [k for k in required if not (values.get(k) or "").strip()]
    if missing:
        raise EnvError(
            f"agents/{agent}/.env is missing required key(s): "
            f"{', '.join(missing)}. Add them to that file; no other file and "
            f"no inherited variable is consulted."
        )

    for name, value in values.items():
        if name in declared:
            os.environ[name] = value

    undeclared = sorted(set(values) - declared)
    return {
        "_agent": agent,
        "_env_file": str(env_path(agent)),
        "_scrubbed": removed,
        "_undeclared": undeclared,
        **{k: v for k, v in values.items() if k in declared},
    }


def secret_values(loaded: dict[str, str]) -> list[str]:
    """The loaded secret values, for the redactor. Never for output."""
    return [v for k, v in loaded.items()
            if not k.startswith("_") and looks_secret(k) and len(str(v)) >= 8]
