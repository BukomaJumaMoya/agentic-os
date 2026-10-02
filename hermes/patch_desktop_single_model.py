#!/usr/bin/env python3
"""Hold the desktop app to the single-model rule the gateway already enforces.

THE GAP
-------
Startup guard condition 9 checks config.yaml when the GATEWAY starts. The
desktop app never runs it, and it does not use config.yaml's model at all once
the composer has a pick: a per-session override (model + provider) travels
from the app's own localStorage into `tui_gateway/server.py:_make_agent`. On
2026-10-02 that sent desktop turns to OpenRouter (402) and Nous (404) while
Hermes' config named custom:deepseek_api/deepseek-flash.

THE FIX
-------
Two edits, both marker-guarded and idempotent, at the only two places a
desktop session gets a model:

  1. _make_agent(), right after _resolve_agent_model_runtime(): the resolved
     provider and model must equal config.yaml's model.provider/model.default,
     and the configured fallback chain must be empty. Otherwise the agent is
     not built and the turn fails with a message naming the mismatch.
  2. _apply_model_switch(), right after switch_model() succeeds: a live
     switch (composer picker, /model) to anything else is refused before it
     touches the agent.

Fail closed: an unreadable config gives an empty expected model, which matches
nothing. Provider names are compared without a leading "custom:", because the
switch path reports `deepseek_api` where config says `custom:deepseek_api`.

WHAT IT DOES NOT COVER
----------------------
  - A fallback added to config.yaml while a desktop chat is already open is
    adopted per turn by _sync_agent_fallback_with_config() without a rebuild.
    The next new or resumed session is refused; that open one is not.
  - Cron jobs and the gateway are not touched here: the gateway has condition
    9, and the paused client-engine jobs deliberately keep a per-job Gemini.

FRAGILITY
---------
Both anchors are single lines of internal code. If an update moves either one,
this script fails with "anchor not found" and post_update.py reports FAIL. It
never applies half a patch, and never reports success on a checkout it did not
patch. `--check` (run by post_update.py after applying) proves both markers
are present.

    python hermes/patch_desktop_single_model.py
    python hermes/patch_desktop_single_model.py --check
"""

from __future__ import annotations

import os
import py_compile
import shutil
import sys
from datetime import datetime
from pathlib import Path

MARKER = "juma-rebuild: desktop single-model rule"

MAKE_AGENT_OLD = """    model, runtime = _resolve_agent_model_runtime(model_override, provider_override)
"""
MAKE_AGENT_NEW = f"""    model, runtime = _resolve_agent_model_runtime(model_override, provider_override)
    # {MARKER}: the desktop composer's per-session pick
    # bypasses config.yaml, and startup guard condition 9 only runs for the
    # gateway. Refuse to build an agent on anything but the configured model,
    # or with a fallback chain configured. Fails closed on an unreadable config.
    _juma_norm = lambda p: str(p or "").strip().lower().removeprefix("custom:")
    _juma_want = (cfg.get("model") or {{}}) if isinstance(cfg, dict) else {{}}
    _juma_got = (_juma_norm(runtime.get("requested_provider") or runtime.get("provider")), str(model or ""))
    if not _juma_want.get("default") or _juma_got != (_juma_norm(_juma_want.get("provider")), str(_juma_want.get("default"))):
        raise RuntimeError(
            f"single-model rule: this session asked for {{_juma_got[0]}}/{{_juma_got[1]}}, but Hermes runs "
            f"only on {{_juma_want.get('provider')}}/{{_juma_want.get('default')}}. Pick that model in the composer.")
    if _load_fallback_model():
        raise RuntimeError("single-model rule: a fallback chain is configured in config.yaml; "
                           "run hermes/configure_providers.py restore")
"""

SWITCH_OLD = """    if not result.success:
        raise ValueError(result.error_message or "model switch failed")
"""
SWITCH_NEW = f"""    if not result.success:
        raise ValueError(result.error_message or "model switch failed")
    # {MARKER}: a live switch (picker, /model) may only
    # land on config.yaml's model. Refused before it touches the agent.
    _juma_norm = lambda p: str(p or "").strip().lower().removeprefix("custom:")
    _juma_want = ((cfg or {{}}).get("model") or {{}})
    if not _juma_want.get("default") or (_juma_norm(result.target_provider), str(result.new_model or "")) != (
            _juma_norm(_juma_want.get("provider")), str(_juma_want.get("default"))):
        raise ValueError(
            f"single-model rule: Hermes runs only on {{_juma_want.get('provider')}}/{{_juma_want.get('default')}}; "
            f"{{result.target_provider}}/{{result.new_model}} is refused.")
"""

EDITS = (
    ("server.py", MAKE_AGENT_OLD, MAKE_AGENT_NEW),
    ("model_switch.py", SWITCH_OLD, SWITCH_NEW),
)


def hermes_home() -> Path:
    home = os.environ.get("HERMES_HOME", "").strip()
    if home:
        return Path(home)
    local = os.environ.get("LOCALAPPDATA", "")
    return (Path(local) if local else Path.home() / "AppData" / "Local") / "hermes"


def tui_dir() -> Path:
    return hermes_home() / "hermes-agent" / "tui_gateway"


def main() -> int:
    check_only = "--check" in sys.argv
    problems: list[str] = []
    for name, old, new in EDITS:
        path = tui_dir() / name
        if not path.exists():
            problems.append(f"{name} not found")
            continue
        text = path.read_text(encoding="utf-8")
        if MARKER in text:
            print(f"  SKIP: {name} already patched")
            continue
        if check_only:
            problems.append(f"{name} is NOT patched")
            continue
        if text.count(old) != 1:
            problems.append(f"anchor not found exactly once in {name}; upstream moved it")
            continue
        backup = path.with_name(f"{name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
        shutil.copy2(path, backup)
        path.write_text(text.replace(old, new, 1), encoding="utf-8")
        try:
            py_compile.compile(str(path), doraise=True)
        except Exception as exc:
            shutil.copy2(backup, path)
            problems.append(f"{name} did not compile ({exc}); restored")
            continue
        print(f"  APPLIED: {name} (backup: {backup.name})")

    if problems:
        print("FAIL: " + "; ".join(problems))
        return 1
    print("OK: the desktop app builds agents only on config.yaml's model, with no fallback")
    return 0


if __name__ == "__main__":
    sys.exit(main())
