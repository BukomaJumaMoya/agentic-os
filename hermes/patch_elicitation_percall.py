#!/usr/bin/env python3
"""Make an MCP approval on Telegram per-call, as its own comment already claims.

THE DEFECT
----------
`tools/approval_prompt.py:request_elicitation_consent()` carries this comment:

    # allow_permanent=False: elicitation is a per-call confirmation -- no
    # pattern to remember.

and passes `allow_permanent=False` on the line below it. That line is on the
**CLI branch**. The GATEWAY branch returns several lines earlier and passes no
such flag, so `run_turn_runner` defaults both flags to True and Telegram renders
"Allow Session" and "🔒 Always Allow" for every MCP write approval.

Measured live: two approvals for `start_code_task` and `github_action` both came
back `choice=always`.

WHY IT MATTERED LESS THAN IT LOOKED, AND STILL MATTERS
------------------------------------------------------
Nothing persists the choice: `resolve_gateway_approval` only resolves the queue
entry, and `request_elicitation_consent` does not write it anywhere. A later
replay test confirmed the behaviour -- the same write twice raised two separate
prompts, and pressing deny on the first genuinely blocked the call.

So the gate was sound and the comment was true, but the button was a lie: it
offered a standing grant that does not exist. An operator who presses "Always
Allow" believes they have changed the system's behaviour and has not. That is
the kind of gap that stops being harmless the moment someone adds persistence.

THE FIX
-------
Two edits, both marker-guarded and idempotent:

  1. request_elicitation_consent() puts allow_permanent/allow_session False
     into the approval data it hands the gateway.
  2. _await_gateway_decision() carries those keys into the payload it publishes.
     It builds `payload` from a fixed key list, so extra keys were silently
     dropped -- which is why (1) alone would not have worked.

Result: an MCP write approval on Telegram offers exactly "Allow Once" and
"Deny". The dangerous-command gate, which legitimately supports session and
permanent grants, is untouched -- only the elicitation path changes.

    python hermes/patch_elicitation_percall.py
    python hermes/patch_elicitation_percall.py --check
"""

from __future__ import annotations

import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

MARKER = "juma-rebuild: elicitation is per-call"

PROMPT_OLD = """            decision = _gw._await_gateway_decision(
                session_key, notify_cb, {"command": message, "description": description,
                                         "pattern_key": "mcp_elicitation",
                                         "pattern_keys": ["mcp_elicitation"]}, surface=surface)"""

PROMPT_NEW = f"""            decision = _gw._await_gateway_decision(
                session_key, notify_cb, {{"command": message, "description": description,
                                         "pattern_key": "mcp_elicitation",
                                         "pattern_keys": ["mcp_elicitation"],
                                         # {MARKER}: the CLI branch below already
                                         # passes allow_permanent=False and says why.
                                         # The gateway branch did not, so Telegram
                                         # offered "Always Allow" for every MCP write
                                         # and returned choice=always -- a standing
                                         # grant that nothing implements.
                                         "allow_permanent": False,
                                         "allow_session": False}}, surface=surface)"""

WAIT_OLD = """        "pattern_keys": list(approval_data.get("pattern_keys", [primary_key])),
        "session_key": session_key, "surface": surface,
    }"""

WAIT_NEW = f"""        "pattern_keys": list(approval_data.get("pattern_keys", [primary_key])),
        "session_key": session_key, "surface": surface,
        # {MARKER}: payload is built from a fixed key list, so a caller asking
        # for a per-call prompt had its flags dropped here and the renderer
        # defaulted both to True. Carried through with the same defaults, so
        # the dangerous-command gate is unaffected.
        "allow_permanent": approval_data.get("allow_permanent", True),
        "allow_session": approval_data.get("allow_session", True),
    }}"""


def hermes_home() -> Path:
    home = os.environ.get("HERMES_HOME", "").strip()
    if home:
        return Path(home)
    local = os.environ.get("LOCALAPPDATA", "")
    return (Path(local) if local else Path.home() / "AppData" / "Local") / "hermes"


def tools_dir() -> Path:
    return hermes_home() / "hermes-agent" / "tools"


EDITS = (
    ("approval_prompt.py", PROMPT_OLD, PROMPT_NEW),
    ("approval_gateway_wait.py", WAIT_OLD, WAIT_NEW),
)


def main() -> int:
    check_only = "--check" in sys.argv
    problems, applied = [], []

    for name, old, new in EDITS:
        path = tools_dir() / name
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
        if old not in text:
            problems.append(f"anchor not found in {name}; upstream moved it")
            continue
        backup = path.with_name(f"{name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
        shutil.copy2(path, backup)
        path.write_text(text.replace(old, new, 1), encoding="utf-8")
        import py_compile
        try:
            py_compile.compile(str(path), doraise=True)
        except Exception as exc:
            shutil.copy2(backup, path)
            problems.append(f"{name} did not compile ({exc}); restored")
            continue
        applied.append(name)
        print(f"  APPLIED: {name} (backup: {backup.name})")

    if problems:
        print("FAIL: " + "; ".join(problems))
        return 1
    print("OK: MCP approvals on the gateway offer Allow Once / Deny only")
    return 0


if __name__ == "__main__":
    sys.exit(main())
