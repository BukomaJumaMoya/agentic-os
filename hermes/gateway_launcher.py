#!/usr/bin/env python3
"""Run the startup guard, then start the gateway -- and make a refusal audible.

THE PROBLEM THIS SOLVES
-----------------------
The guard worked and nobody could tell. `gateway/run.py` logs
"Telegram tool-surface guard passed" at INFO and "REFUSING TO START" at
CRITICAL, and NEITHER LINE APPEARS ANYWHERE. Checked directly:

    Select-String ~/AppData/Local/hermes/logs/gateway.log -Pattern "tool-surface"
    -> no matches, across every start in the file

The guard sits at the top of `start_gateway()`, which runs before Hermes
attaches its file handlers, so both verdicts go to a logger with nowhere to
write. The failure mode that leaves is the worst available one: the gateway
refuses to start, exits non-zero, Task Scheduler retries it a few times and
gives up, and the first sign is that Telegram has gone quiet -- which looks
exactly like a flat phone battery or a dropped connection.

So the verdict is emitted HERE, before Hermes' logging exists to be a problem,
on three independent channels: a Telegram message, the Windows Event Log, and a
plain file. Three because this is the path that tells you the security boundary
broke, and the previous count was zero.

WHY IN FRONT OF THE GATEWAY RATHER THAN INSIDE IT
-------------------------------------------------
A refusal has to be reported by something that is still running afterwards. The
in-process guard cannot report its own refusal reliably: it is inside the thing
that is about to exit. Running the check out here, first, means the reporter
outlives the verdict.

The in-process guard STAYS. It is not redundant -- it covers every other route
into `start_gateway()` (`hermes gateway start`, `hermes gateway restart`, an
embedded caller), none of which come through this file. This one covers the
scheduled task, which is the only route that runs unattended, which is the only
route where nobody is watching the terminal.

    python hermes/gateway_launcher.py              # guard, then start
    python hermes/gateway_launcher.py --check      # guard only, print, exit
    python hermes/gateway_launcher.py --test-alert # prove the alert path works
    python hermes/gateway_launcher.py --install    # point the task at this file
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CHECKER = REPO / "hermes" / "check_telegram_surface.py"

EVENT_SOURCE = "Hermes_Gateway"
EVENT_ID_REFUSED = 101
EVENT_ID_PASSED = 100

# Fixed text. It says that the boundary failed and that nothing is running; it
# names no tool, no config key and no value, because this goes over Telegram to
# a phone and the whole point of the guard is that the surface may be open.
# Whoever gets this reads the machine, not the message.
ALERT_TEXT = (
    "HERMES GATEWAY REFUSED TO START.\n\n"
    "A startup safety check failed, so the gateway did not start and no "
    "messages will be answered until it is fixed.\n\n"
    "This alert is sent by the launcher and carries no details on purpose. "
    "Run this on the machine:\n\n"
    "    python hermes/post_update.py"
)


def hermes_home() -> Path:
    home = os.environ.get("HERMES_HOME", "").strip()
    if home:
        return Path(home)
    local = os.environ.get("LOCALAPPDATA", "")
    return (Path(local) if local else Path.home() / "AppData" / "Local") / "hermes"


def hermes_python() -> Path:
    return hermes_home() / "hermes-agent" / "venv" / "Scripts" / "python.exe"


def guard_log() -> Path:
    return hermes_home() / "logs" / "gateway-guard.log"


def read_env(name: str) -> str:
    """One value out of Hermes' .env. Callers pass it to an API; nothing
    prints it, and nothing here returns it anywhere it could be logged."""
    envfile = hermes_home() / ".env"
    if not envfile.exists():
        return ""
    for line in envfile.read_text(encoding="utf-8", errors="replace").splitlines():
        match = re.match(rf"\s*{re.escape(name)}\s*=(.*)$", line)
        if match:
            return match.group(1).strip().strip('"').strip("'")
    return ""


# --------------------------------------------------------------------------
# the three channels
# --------------------------------------------------------------------------

def notify_file(verdict: str, detail: str) -> None:
    """Always works. The other two can be unregistered or offline."""
    path = guard_log()
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().isoformat(timespec="seconds")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{stamp} {verdict} {detail}\n")


def notify_event_log(verdict: str, detail: str) -> str:
    """Windows Event Log, Application channel.

    Writing needs the source registered, and REGISTERING one needs elevation --
    a standard user gets "Access is denied" from both eventcreate and
    New-EventLog. So this reports whether it wrote rather than pretending: a
    channel that silently does nothing is how the guard became invisible in the
    first place. Register it once, elevated:

        New-EventLog -LogName Application -Source Hermes_Gateway
    """
    level = "Error" if verdict == "REFUSED" else "Information"
    event_id = EVENT_ID_REFUSED if verdict == "REFUSED" else EVENT_ID_PASSED
    message = f"Hermes gateway startup guard: {verdict}. {detail}"
    script = (
        f"Write-EventLog -LogName Application -Source '{EVENT_SOURCE}' "
        f"-EntryType {level} -EventId {event_id} -Message @'\n{message}\n'@"
    )
    try:
        done = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=30)
    except Exception as exc:
        return f"event log: not written ({type(exc).__name__})"
    if done.returncode == 0:
        return "event log: written"
    reason = (done.stderr or "").strip().splitlines()
    hint = reason[0][:120] if reason else f"exit {done.returncode}"
    return (f"event log: NOT written ({hint}); register the source once, "
            f"elevated: New-EventLog -LogName Application -Source {EVENT_SOURCE}")


def notify_telegram() -> str:
    """One fixed-text message to the one authorised chat.

    The token comes out of Hermes' .env and goes straight into the request. It
    is never logged, never returned, and never included in the message body.
    """
    token = read_env("TELEGRAM_BOT_TOKEN")
    chat = read_env("TELEGRAM_ALLOWED_USERS").split(",")[0].strip()
    if not token or not chat:
        missing = [n for n, v in (("TELEGRAM_BOT_TOKEN", token),
                                  ("TELEGRAM_ALLOWED_USERS", chat)) if not v]
        return f"telegram: not sent ({', '.join(missing)} not in Hermes' .env)"

    payload = json.dumps({"chat_id": chat, "text": ALERT_TEXT}).encode()
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.load(response)
        return ("telegram: sent" if body.get("ok")
                else f"telegram: rejected by the Bot API (ok={body.get('ok')})")
    except urllib.error.HTTPError as exc:
        # The body can echo the token back in some error shapes, so only the
        # status code is reported.
        return f"telegram: NOT sent (HTTP {exc.code})"
    except Exception as exc:
        return f"telegram: NOT sent ({type(exc).__name__})"


def announce(verdict: str, detail: str) -> list[str]:
    results = []
    notify_file(verdict, detail)
    results.append(f"file: {guard_log()}")
    if verdict == "REFUSED":
        results.append(notify_telegram())
    results.append(notify_event_log(verdict, detail))
    return results


# --------------------------------------------------------------------------

def drop_stale_schema_cache() -> None:
    """Delete Hermes' MCP schema cache before every start. It is DERIVED data.

    The cache is keyed per MCP server and only re-derived when that server's
    fingerprint changes, and the fingerprint does not cover tool ANNOTATIONS.
    So entries computed by older code survive indefinitely: after the
    readOnlyHint fix, a newly added server got the correct hint while the three
    existing servers kept `readOnlyHint: false` for every read tool, and guard
    condition 8 refused the start -- correctly, but over a cache rather than
    over the running system.

    A derived artefact must never be able to wedge the gateway. Hermes rebuilds
    this on the next connect, so dropping it costs one discovery round and
    removes the whole staleness class. Condition 8 keeps its teeth: after the
    rebuild it compares hints the CURRENT code computed from the LIVE agents,
    which is the disagreement worth refusing over.
    """
    cache = hermes_home() / "cache" / "mcp_schema_cache.json"
    try:
        if cache.exists():
            cache.unlink()
            notify_file("INFO", f"dropped {cache.name} (derived; rebuilt on connect)")
    except Exception as exc:                      # noqa: BLE001 - never block a start
        notify_file("INFO", f"could not drop {cache.name}: {type(exc).__name__}")


def run_guard() -> tuple[bool, str]:
    """The same check the in-process guard runs, under Hermes' interpreter.

    Fails closed: if it cannot be run at all, that is a refusal. An unprovable
    boundary is treated as a breached one.
    """
    python = hermes_python()
    if not python.exists():
        return False, f"Hermes' interpreter not found at {python}"
    try:
        done = subprocess.run(
            [str(python), str(CHECKER)],
            cwd=str(hermes_home() / "hermes-agent"),
            env=dict(os.environ, PYTHONIOENCODING="utf-8",
                     HERMES_HOME=str(hermes_home())),
            capture_output=True, text=True, timeout=180)
    except Exception as exc:
        return False, f"the guard could not be run ({type(exc).__name__}: {exc})"
    message = (done.stdout or done.stderr or "").strip() or "(no output)"
    return done.returncode == 0, message


def start_gateway() -> int:
    """Hand off to the gateway, detached, exactly as the VBS used to."""
    command = [str(hermes_python()), "-m", "hermes_cli.main", "gateway", "run"]
    creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen(command, cwd=str(hermes_home()), creationflags=creation,
                     stdin=subprocess.DEVNULL)
    return 0


def install() -> int:
    """Point the scheduled task's launcher at this file, and fix its policy.

    `hermes gateway install` regenerates the VBS from a template, which would
    drop this -- the same way `hermes update` stashes the two source patches.
    So this is re-run by hermes/post_update.py rather than done once by hand.
    """
    vbs = hermes_home() / "gateway-service" / "Hermes_Gateway.vbs"
    if not vbs.exists():
        print(f"FAIL: {vbs} not found")
        return 1

    text = vbs.read_text(encoding="utf-8")
    launcher = f'"{hermes_python()}" "{Path(__file__).resolve()}"'
    wanted = f'sh.Run "{launcher}", 0, False'

    if wanted in text:
        print("  launcher already installed in Hermes_Gateway.vbs")
    else:
        backup = vbs.with_name(
            f"Hermes_Gateway.vbs.bak-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
        backup.write_text(text, encoding="utf-8")
        lines = [l for l in text.splitlines() if not l.startswith("sh.Run ")]
        if len(lines) == len(text.splitlines()):
            print("FAIL: no sh.Run line in the VBS; upstream changed the template")
            return 1
        vbs.write_text("\n".join(lines + [wanted]) + "\n", encoding="utf-8")
        print(f"  Hermes_Gateway.vbs now runs the guard first (backup: {backup.name})")

    return check_task_policy()


# Three tries a minute apart. The task shipped with Count=999, which is not a
# supervision policy -- it is a machine that retries a broken gateway every
# minute forever and never tells anyone, which is precisely the silence the
# alerting above exists to break.
WANT_RESTART_COUNT = 3
WANT_RESTART_INTERVAL = "PT1M"

# Writing to Hermes_Gateway needs elevation: Set-ScheduledTask, schtasks
# /create /xml /f and Register-ScheduledTask all return "Access is denied" for
# a standard user, so this is reported rather than attempted-and-swallowed.
ELEVATED_TASK_FIX = (
    "$t = Get-ScheduledTask -TaskName Hermes_Gateway; $s = $t.Settings; "
    f"$s.RestartCount = {WANT_RESTART_COUNT}; "
    f"$s.RestartInterval = '{WANT_RESTART_INTERVAL}'; "
    "Set-ScheduledTask -TaskName Hermes_Gateway -Settings $s"
)


def check_task_policy() -> int:
    """Report the scheduled task's three required properties.

    Returns 0 when all three hold. It does not try to fix the two that need
    elevation, because a fix that silently fails is worse than a report that
    says what is wrong -- the whole reason this file exists.
    """
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command",
         "$t = Get-ScheduledTask -TaskName Hermes_Gateway; "
         "$s = $t.Settings; "
         "$trigger = ($t.Triggers | ForEach-Object { $_.CimClass.CimClassName }) -join ','; "
         "$action = $t.Actions[0].Arguments; "
         "[pscustomobject]@{ restart_count = $s.RestartCount; "
         "restart_interval = [string]$s.RestartInterval; triggers = $trigger; "
         "action = $action } | ConvertTo-Json -Compress"],
        capture_output=True, text=True, timeout=60)
    try:
        state = json.loads((result.stdout or "").strip())
    except Exception:
        print(f"  task policy: could not be read ({(result.stderr or '').strip()[:160]})")
        return 1

    problems = []
    at_logon = "LogonTrigger" in str(state.get("triggers", ""))
    print(f"  starts at logon:   {'yes' if at_logon else 'NO'} ({state.get('triggers')})")
    if not at_logon:
        problems.append("no logon trigger")

    count, interval = state.get("restart_count"), state.get("restart_interval")
    ok_restart = count == WANT_RESTART_COUNT and interval == WANT_RESTART_INTERVAL
    print(f"  restart on failure: {count} tries, {interval} apart "
          f"({'ok' if ok_restart else f'want {WANT_RESTART_COUNT} / {WANT_RESTART_INTERVAL}'})")
    if not ok_restart:
        problems.append("restart policy")

    runs_guard = "Hermes_Gateway.vbs" in str(state.get("action", ""))
    print(f"  runs the guard:    {'yes, via the VBS -> this launcher' if runs_guard else 'NO'}")
    if not runs_guard:
        problems.append("the task does not reach this launcher")

    if "restart policy" in problems:
        print("\n  The restart policy needs ONE elevated command (the task's ACL "
              "denies a standard user; Set-ScheduledTask, schtasks /create /xml "
              "/f and Register-ScheduledTask all return Access is denied):\n")
        print(f"    {ELEVATED_TASK_FIX}\n")
    return 0 if not problems else 1


def main() -> int:
    if "--install" in sys.argv:
        return install()

    if "--test-alert" in sys.argv:
        # Exercises the real alert path -- real Bot API call, real Event Log
        # attempt -- without breaking anything to do it.
        print("sending the refusal alert as a test (nothing is actually broken)")
        for line in announce("REFUSED", "TEST -- launcher --test-alert, not a real refusal"):
            print(f"  {line}")
        return 0

    drop_stale_schema_cache()
    ok, message = run_guard()
    verdict = "PASSED" if ok else "REFUSED"
    print(f"{verdict}: {message}")
    for line in announce(verdict, message):
        print(f"  {line}")

    if "--check" in sys.argv:
        return 0 if ok else 1
    if not ok:
        print("gateway NOT started")
        return 1
    return start_gateway()


if __name__ == "__main__":
    sys.exit(main())
