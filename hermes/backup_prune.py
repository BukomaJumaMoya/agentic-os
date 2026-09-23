#!/usr/bin/env python3
"""Keep the last few config backups and delete the rest.

WHY THIS IS A SECURITY CHORE, NOT HOUSEKEEPING
----------------------------------------------
Every script in this directory copies `config.yaml` aside before editing it,
which is the right instinct and was never cleaned up. Measured on 2026-09-23:

    config.yaml.bak-* : 49 files, 377 KB
                        49 of 49 contained a live-shaped credential

That is forty-nine plaintext copies of the Telegram bot token, none of them
covered by the approval patch -- which matches `config.yaml`, `.env`,
`auth.json` and `mcp-tokens`, not `config.yaml.bak-20260921-110148`. Rotating
the token fixes the live one and leaves the other forty-nine.

A backup is worth keeping for as long as it might be restored, which in
practice is the last one or two. Beyond that it is an unindexed copy of a
secret with no expiry.

KEEP is 3: enough to step back past a bad edit and the edit before it, few
enough that the pile cannot grow again.

Every backup-making script here calls prune() immediately after writing one,
so the count is bounded at the moment it would otherwise grow.
"""

from __future__ import annotations

import sys
from pathlib import Path

KEEP = 3


def prune(directory: Path, pattern: str, keep: int = KEEP,
          *, quiet: bool = False) -> list[str]:
    """Delete all but the `keep` most recent files matching `pattern`.

    Newest is decided by mtime, not by the timestamp in the filename: a name
    can be hand-edited and a clock can go backwards, but the thing being
    protected is "restore what was most recently replaced".

    Returns the names removed. A file that cannot be deleted is reported and
    skipped -- pruning must never be the reason a config script fails.
    """
    try:
        found = sorted(Path(directory).glob(pattern),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return []

    removed: list[str] = []
    for stale in found[keep:]:
        try:
            stale.unlink()
            removed.append(stale.name)
        except OSError as exc:            # noqa: PERF203 - report and continue
            if not quiet:
                print(f"  could not remove {stale.name}: {exc}")
    if removed and not quiet:
        print(f"  pruned {len(removed)} old backup(s), kept the newest {keep}")
    return removed


def main() -> int:
    """Standalone sweep, for a home that has already accumulated a pile."""
    import os
    home = os.environ.get("HERMES_HOME", "").strip()
    if home:
        base = Path(home)
    else:
        local = os.environ.get("LOCALAPPDATA", "")
        base = (Path(local) if local else Path.home() / "AppData" / "Local") / "hermes"

    total = 0
    for pattern in ("config.yaml.bak-*", "cli_toolsets.stash.json.bak-*"):
        removed = prune(base, pattern)
        total += len(removed)
    for pattern in ("*.py.bak-*", "*.py.pre-juma-patch"):
        total += len(prune(base / "hermes-agent" / "tools", pattern))
    for pattern in ("*.py.bak-*", "Hermes_Gateway.vbs.bak-*"):
        total += len(prune(base / "hermes-agent" / "gateway", pattern))
        total += len(prune(base / "gateway-service", pattern))

    remaining = len(list(base.glob("config.yaml.bak-*")))
    print(f"removed {total} file(s); {remaining} config backup(s) remain")
    return 0


if __name__ == "__main__":
    sys.exit(main())
