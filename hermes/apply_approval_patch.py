#!/usr/bin/env python3
"""Re-apply the Hermes approval patch: reading the Hermes secret store asks.

WHY THIS FILE EXISTS RATHER THAN A ONE-OFF EDIT
-----------------------------------------------
Hermes is installed as a git checkout and `hermes update` pulls into it with
updates.non_interactive_local_changes = stash. A hand edit to a tracked file is
therefore not "applied" in any durable sense -- it is applied until the next
update stashes it, silently, with the agent still running and the rule gone.
That is exactly what happened to the previous patch: after the update the
verdict for `cat ~/.hermes/.env` was `allow`, and nothing said so.

So the patch lives here, in the repository that is actually backed up, and is
re-applied by running this script. It is idempotent: it looks for its own
marker and does nothing if the rules are already present.

WHAT IT ADDS
------------
Upstream already treats a WRITE to ~/.hermes/.env as dangerous
(_HERMES_ENV_PATH feeds _SENSITIVE_WRITE_TARGET). A READ was not covered at
all, and the read is the whole prize: every provider key, the Telegram bot
token and the allow-list are in that one file.

The rules match on the PATH, not on a read verb, because the set of read verbs
is unbounded -- cat, type, more, less, head, tail, strings, od, xxd, grep, awk,
sed -n, findstr, certutil, Get-Content, gc, Select-String, Import-Csv,
`python -c open(...)`, `node -e readFileSync(...)`, and whatever ships next
year. The path has no innocent use inside an agent-issued command, so its
appearance anywhere in one is the signal. That is the same reasoning upstream
already applies to the cloud instance-metadata endpoints.

Verdict is ask-approval, not a hardline deny: the operator inspecting or
editing their own secrets through the agent is legitimate. It must never be
silent, which is a different requirement from never happening.

Scope note: auth.json and mcp-tokens/ sit beside .env in the same directory and
hold the same class of material (OAuth tokens, MCP server credentials). Copying
the keys out of auth.json instead of .env would defeat a .env-only rule, so all
three are covered.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

MARKER = "juma-rebuild: Hermes secret-store READ rules"

# Insertion point: the comment that closes the Windows-specific tier of
# DANGEROUS_PATTERNS. Located by its ASCII tail so a change to the box-drawing
# rule above it does not break the match.
ANCHOR_SUBSTRING = "end of Windows tier"

RULES = r"""    # ---------------------------------------------------------------------
    # juma-rebuild: Hermes secret-store READ rules
    # Upstream covers WRITES to ~/.hermes/.env via _SENSITIVE_WRITE_TARGET.
    # Reads were uncovered, and the read is the prize: provider keys, the
    # Telegram bot token and the allow-list all live in that one file.
    #
    # Matched on the PATH, never on a read verb -- the verb list is unbounded
    # (cat/type/more/head/tail/strings/xxd/grep/sed -n/findstr/Get-Content/
    # Select-String/python -c open()/node -e readFileSync()/...). The path has
    # no innocent use in an agent-issued command, so its appearance anywhere in
    # one is the signal, exactly as for the cloud-metadata endpoints below.
    #
    # ask-approval, not hardline: the operator reading their own secrets is
    # legitimate; doing it silently is not.
    #
    # _normalize_command_for_detection() folds the resolved home into the
    # ~/.hermes/ spelling, so the absolute Windows path, the forward-slash form
    # and the tilde form all reach the matcher as ~/.hermes/.env.
    (r'(?:~|\$home|\$\{home\})[\/]\.hermes[\/](?:\.env|auth\.json|mcp-tokens)',
     "read Hermes secret store"),
    (r'(?:\$hermes_home|\$\{hermes_home\}|%hermes_home%)[\/]*(?:\.env|auth\.json|mcp-tokens)',
     "read Hermes secret store (HERMES_HOME)"),
    # PowerShell/cmd environment spellings. \bappdata cannot fire inside
    # "LOCALAPPDATA" (no word boundary mid-token), and normalisation strips the
    # backslashes from this form, so both spellings are matched explicitly.
    (r'\$env:(?:localappdata|appdata|userprofile)[\/]*hermes[\/]*(?:\.env|auth\.json|mcp-tokens)',
     "read Hermes secret store (PowerShell env path)"),
    (r'%(?:localappdata|appdata|userprofile)%[\/]*hermes[\/]*(?:\.env|auth\.json|mcp-tokens)',
     "read Hermes secret store (cmd env path)"),
    # Raw, un-normalised Windows path, for any route that reaches the matcher
    # before normalisation folds it.
    (r'appdata[\/](?:local|roaming)[\/]hermes[\/](?:\.env|auth\.json|mcp-tokens)',
     "read Hermes secret store (Windows path)"),
    # end juma-rebuild
    # ---------------------------------------------------------------------
"""


def target_file() -> Path:
    install = os.environ.get("HERMES_INSTALL_DIR", "").strip()
    if install:
        return Path(install) / "tools" / "approval_detection.py"
    home = os.environ.get("HERMES_HOME", "").strip()
    base = Path(home) if home else Path(os.environ.get("LOCALAPPDATA", Path.home())) / "hermes"
    return base / "hermes-agent" / "tools" / "approval_detection.py"


def apply(path: Path) -> str:
    if not path.exists():
        return f"FAIL: {path} does not exist"
    text = path.read_text(encoding="utf-8")
    if MARKER in text:
        return f"SKIP: already patched ({path})"

    lines = text.splitlines(keepends=True)
    anchors = [i for i, line in enumerate(lines)
               if ANCHOR_SUBSTRING in line and line.lstrip().startswith("#")]
    if len(anchors) != 1:
        return (f"FAIL: expected exactly one {ANCHOR_SUBSTRING!r} comment in "
                f"{path.name}, found {len(anchors)}. Upstream moved it; "
                f"re-derive the insertion point before trusting this script.")

    backup = path.with_suffix(".py.pre-juma-patch")
    if not backup.exists():
        backup.write_text(text, encoding="utf-8")

    lines.insert(anchors[0], RULES)
    path.write_text("".join(lines), encoding="utf-8")
    return f"APPLIED: {path} (backup at {backup.name})"


def main() -> int:
    path = target_file()
    result = apply(path)
    print(result)
    if result.startswith("FAIL"):
        return 1
    # Compile-check: a broken regex here would disable the whole detector.
    import py_compile
    try:
        py_compile.compile(str(path), doraise=True)
    except Exception as exc:
        print(f"FAIL: patched file does not compile: {exc}")
        return 1
    for pattern, _desc in _extract_added(path):
        try:
            re.compile(pattern, re.IGNORECASE | re.DOTALL)
        except re.error as exc:
            print(f"FAIL: added pattern does not compile: {pattern!r}: {exc}")
            return 1
    print("OK: file compiles and every added pattern compiles")
    return 0


def _extract_added(path: Path):
    """The (pattern, description) pairs this script added, for checking.

    Patterns are read back out of the file and compiled rather than trusted:
    a bad regex here would raise at Hermes import time and take the WHOLE
    dangerous-command detector down with it, which fails open.
    """
    import ast
    text = path.read_text(encoding="utf-8")
    block = text.split(MARKER, 1)[-1].split("# end juma-rebuild", 1)[0]
    for line in block.splitlines():
        stripped = line.strip()
        if not stripped.startswith("(r" + chr(39)):
            continue
        literal = stripped[1:].rstrip(",")
        try:
            yield ast.literal_eval(literal), None
        except Exception as exc:
            raise SystemExit(f"FAIL: cannot read back pattern {literal!r}: {exc}")


if __name__ == "__main__":
    sys.exit(main())
