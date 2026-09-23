#!/usr/bin/env python3
"""Run this after every `hermes update`. One command, PASS or FAIL at the end.

WHY IT HAS TO EXIST
-------------------
Hermes is installed as a git checkout, and `hermes update` pulls into it with
`updates.non_interactive_local_changes: stash`. Every local change to that
checkout is therefore temporary by default. This has already happened once, to
the approval patch: after an update the verdict for `cat %LOCALAPPDATA%\hermes\.env` was
back to `allow`, the agent kept running, and nothing anywhere said so.

Two of the five invariants live inside that checkout -- the approval patch and
the gateway's in-process tool-surface guard -- so both are one `hermes update`
away from silently not existing. A third, the launcher that reports a refusal,
lives in a file `hermes gateway install` regenerates from a template.

The defence is not to remember. It is to have one command that puts all of it
back and then PROVES it, and to run that command every time.

WHAT IT RUNS, IN THIS ORDER
---------------------------
  1. configure_orchestrator.py    the three MCP servers and their include lists
  2. harden_telegram_surface.py   the Telegram allowlist + the in-process guard
  3. configure_gemini.py restore  the model chain, and the reasoning-effort trap
  4. apply_approval_patch.py      secret-store reads require approval again
  5. patch_readonly_hint.py       Hermes reads readOnlyHint at all (see its docstring)
  6. gateway_launcher.py --install  the task reaches the guard; policy reported
  7. check_telegram_surface.py    all eight conditions, under Hermes' interpreter
  8. tests/test_surface_guard.py  the negative tests -- proof it still REFUSES

7 before 8 on purpose. 7 says the machine is currently in the right state; 8
says the check that decided that is still capable of saying no. A green 7 with
a broken 8 is the failure mode that matters, because it looks exactly like
success.

    python hermes/post_update.py
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
AGENT_PYTHON = REPO / "agents" / ".venv" / "Scripts" / "python.exe"


def hermes_home() -> Path:
    home = os.environ.get("HERMES_HOME", "").strip()
    if home:
        return Path(home)
    local = os.environ.get("LOCALAPPDATA", "")
    return (Path(local) if local else Path.home() / "AppData" / "Local") / "hermes"


def hermes_python() -> Path:
    return hermes_home() / "hermes-agent" / "venv" / "Scripts" / "python.exe"


# (label, script, args, interpreter, cwd, required)
#
# `required=False` marks a step whose failure is a finding to report rather
# than a reason to stop: the launcher install reports the one piece of the
# scheduled-task policy a standard user cannot write, and stopping there would
# skip the checks that actually matter.
def steps() -> list[tuple]:
    agent, hermes = AGENT_PYTHON, hermes_python()
    checkout = hermes_home() / "hermes-agent"
    return [
        ("MCP servers and include lists",
         REPO / "hermes" / "configure_orchestrator.py", [], agent, None, True),
        ("Telegram surface + in-process guard",
         REPO / "hermes" / "harden_telegram_surface.py", [], agent, None, True),
        ("model chain",
         REPO / "hermes" / "configure_gemini.py", ["restore"], agent, None, True),
        ("approval patch",
         REPO / "hermes" / "apply_approval_patch.py", [], agent, None, True),
        ("readOnlyHint alias patch",
         REPO / "hermes" / "patch_readonly_hint.py", [], agent, None, True),
        ("elicitation per-call patch",
         REPO / "hermes" / "patch_elicitation_percall.py", [], agent, None, True),
        ("gateway launcher + task policy",
         REPO / "hermes" / "gateway_launcher.py", ["--install"], agent, None, False),
        ("startup guard (all eight conditions)",
         REPO / "hermes" / "check_telegram_surface.py", [], hermes, checkout, True),
        ("guard negative tests",
         REPO / "tests" / "test_surface_guard.py", [], agent, None, True),
    ]


def run(label: str, script: Path, args: list[str], python: Path,
        cwd: Path | None) -> tuple[bool, str]:
    if not python.exists():
        return False, f"interpreter not found: {python}"
    if not script.exists():
        return False, f"script not found: {script}"
    done = subprocess.run(
        [str(python), str(script), *args],
        cwd=str(cwd) if cwd else str(REPO),
        env=dict(os.environ, PYTHONIOENCODING="utf-8",
                 HERMES_HOME=str(hermes_home())),
        capture_output=True, text=True)
    output = ((done.stdout or "") + (done.stderr or "")).rstrip()
    return done.returncode == 0, output


def main() -> int:
    print(f"post-update: {REPO}")
    print(f"hermes home: {hermes_home()}\n")

    results: list[tuple[str, bool, bool]] = []
    for label, script, args, python, cwd, required in steps():
        print(f"== {label} ==")
        ok, output = run(label, script, args, python, cwd)
        for line in (output or "(no output)").splitlines():
            print(f"   {line}")
        print(f"   -> {'ok' if ok else 'FAILED'}\n")
        results.append((label, ok, required))

    failed = [label for label, ok, required in results if not ok and required]
    advisory = [label for label, ok, required in results if not ok and not required]

    print("=" * 68)
    for label, ok, required in results:
        mark = "PASS" if ok else ("FAIL" if required else "WARN")
        print(f"  {mark}  {label}")
    print("=" * 68)

    if advisory:
        print(f"\n{len(advisory)} step(s) need attention but do not block: "
              f"{', '.join(advisory)}")
        print("See their output above; the launcher prints the exact command.")

    if failed:
        print(f"\nFAIL -- {len(failed)} required step(s) failed: {', '.join(failed)}")
        print("The gateway must not be started until these pass. The startup "
              "guard will refuse anyway, and now it will say so over Telegram.")
        return 1

    print("\nPASS -- every patch re-applied, all eight guard conditions hold, "
          "and the guard still refuses each of them when broken.")
    print("Restart the gateway to pick this up: hermes gateway restart")
    return 0


if __name__ == "__main__":
    sys.exit(main())
