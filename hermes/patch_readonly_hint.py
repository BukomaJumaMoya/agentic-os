#!/usr/bin/env python3
"""Make Hermes actually read `readOnlyHint`, so its write-approval gate works.

THE DEFECT
----------
Hermes gates write-capable MCP tools on a `trust: untrusted` server, and decides
which tools those are from the `readOnlyHint` annotation each server publishes
(`tools/mcp_tool_handlers.py:_trust_gate_check`). It reads the annotation here:

    tools/mcp_tool_registration.py:_annotation_read_only_hint()
        getattr(annotations, "readOnlyHint", None)

The MCP SDK's `ToolAnnotations` is a pydantic model whose PYTHON ATTRIBUTE is
snake_case; `readOnlyHint` is only the serialization alias. Measured against a
live agent, under Hermes' own interpreter:

    annotations object : ToolAnnotations(title=None, read_only_hint=True, ...)
    read_only_hint     : True
    model_dump(by_alias) : {'readOnlyHint': True, ...}
    Hermes verdict     : _annotation_read_only_hint -> False

So `getattr(ann, "readOnlyHint")` is always None, the hint is always False, and
EVERY tool on an untrusted server is classified write-capable.

WHY IT MATTERED HERE
--------------------
It fails closed, so nothing became unsafe -- but everything became ungated-able.
The daily-briefing cron job died on its first real run with:

    Tool mcp__pm__pm_query returned error: "The user did not approve running
    write-capable MCP tool 'pm_query' on untrusted server 'pm'."

`pm_query` is annotated `readOnlyHint: True` and is read-only. Every unattended
workflow is unrunnable while this holds, because an unattended run has nobody to
approve anything, and the whole point of annotating the read tools was to let
reads through.

Two of our own checks passed while the system was broken, which is the part
worth remembering: `tests/test_surface_guard.py` asserts what the AGENTS
publish, and the gate probe asserts what the GATE does with hints it is handed.
Neither exercised the step between them. The same shape as the original toolset
collision -- two correct descriptions of one surface, never compared.

THE FIX
-------
Accept both spellings, and the cache dict's spelling too. Unknown still means
write-capable, so the fail-closed default is untouched; this only stops a hint
that IS present from being dropped.

Upstream-reportable; see documentation/upstream-issue-readonlyhint-alias.md.

    python hermes/patch_readonly_hint.py
    python hermes/patch_readonly_hint.py --check
"""

from __future__ import annotations

import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

MARKER = "juma-rebuild: readOnlyHint alias"

OLD = ('    hint = annotations.get("readOnlyHint") if isinstance(annotations, dict) '
       'else getattr(annotations, "readOnlyHint", None)\n')

NEW = f'''    # --- {MARKER} ---------------------------------------
    # The SDK's ToolAnnotations is a pydantic model whose attribute is
    # snake_case (`read_only_hint`); `readOnlyHint` is only the serialization
    # alias. Reading the alias off the object always yields None, so every tool
    # was classified write-capable and every read on a `trust: untrusted`
    # server needed approval -- which no unattended cron run can give.
    # Both spellings are accepted here, for the SDK object and for the cache
    # dict. `unknown -> write-capable` is deliberately unchanged.
    if isinstance(annotations, dict):
        hint = annotations.get("readOnlyHint")
        if hint is None:
            hint = annotations.get("read_only_hint")
    else:
        hint = getattr(annotations, "readOnlyHint", None)
        if hint is None:
            hint = getattr(annotations, "read_only_hint", None)
    # --- end {MARKER} -----------------------------------
'''


def hermes_home() -> Path:
    home = os.environ.get("HERMES_HOME", "").strip()
    if home:
        return Path(home)
    local = os.environ.get("LOCALAPPDATA", "")
    return (Path(local) if local else Path.home() / "AppData" / "Local") / "hermes"


def target() -> Path:
    return hermes_home() / "hermes-agent" / "tools" / "mcp_tool_registration.py"


def schema_cache() -> Path:
    return hermes_home() / "cache" / "mcp_schema_cache.json"


def main() -> int:
    path = target()
    if not path.exists():
        print(f"FAIL: {path} not found")
        return 1

    text = path.read_text(encoding="utf-8")
    if MARKER in text:
        print(f"SKIP: already patched ({path.name})")
    elif "--check" in sys.argv:
        print(f"FAIL: {path.name} is NOT patched; readOnlyHint is being ignored")
        return 1
    else:
        if OLD not in text:
            print(f"FAIL: anchor not found in {path.name}. Upstream changed "
                  f"_annotation_read_only_hint(); re-derive before rerunning.")
            return 1
        backup = path.with_name(
            f"mcp_tool_registration.py.bak-{datetime.now():%Y%m%d-%H%M%S}")
        shutil.copy2(path, backup)
        path.write_text(text.replace(OLD, NEW, 1), encoding="utf-8")
        print(f"APPLIED: {path.name} (backup: {backup.name})")

        import py_compile
        try:
            py_compile.compile(str(path), doraise=True)
        except Exception as exc:
            shutil.copy2(backup, path)
            print(f"FAIL: patched file does not compile ({exc}); restored")
            return 1

    # The discovery cache stores the COMPUTED hint, so a cache written while the
    # bug was live says readOnlyHint: false for every tool and would survive the
    # patch. Drop it; Hermes rebuilds it on the next connect.
    cache = schema_cache()
    if "--check" not in sys.argv and cache.exists():
        cache.unlink()
        print(f"cleared {cache.name} (it holds hints computed by the old code)")

    print("OK: readOnlyHint is read from both the SDK object and the cache dict")
    return 0


if __name__ == "__main__":
    sys.exit(main())
