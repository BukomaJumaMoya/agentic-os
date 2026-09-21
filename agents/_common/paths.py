#!/usr/bin/env python3
"""Filesystem confinement.

This is the archived agents/coding/main.py resolve_write_target logic, kept
whole because each of its checks was added for a reason that still applies, and
generalised from one hard-coded workspace to any declared root.

THE BUG IT EXISTS TO PREVENT
----------------------------
    Path(output_dir) / filename

discards `output_dir` entirely when `filename` is absolute. That single line of
pathlib behaviour is what turned a "write into the workspace" primitive into an
arbitrary-write primitive. Nothing about the call site looked wrong.

So the checks run BEFORE resolution, on the raw strings:

  - absolute POSIX paths and UNC prefixes (/x, \\\\server\\share)
  - drive letters (C:), which defeat a leading-slash check on Windows
  - '..' traversal in any segment
  - Windows reserved device names (con, nul, com1..9, lpt1..9), with or
    without an extension: writing to those hits a device, not a file

...and then, after resolution, the result is confirmed to still be under the
root. Both halves are needed: the pre-checks catch what resolution would
normalise away, and the post-check catches symlinks and anything the
pre-checks did not imagine.

confine_existing() is the read-side twin, for the coding agent's project_path:
the caller names a directory that must already exist inside the allowed root.
It resolves symlinks first, because a symlink inside the root pointing outside
it is precisely the interesting case.
"""

from __future__ import annotations

import re
from pathlib import Path

_RESERVED_DEVICE_NAMES = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


class ConfinementError(ValueError):
    """A path was rejected before anything was opened."""


def _check_relative_segment(label: str, raw: str) -> list[str]:
    text = str(raw).strip()
    if not text:
        raise ConfinementError(f"{label} must not be empty")
    if text.startswith(("/", "\\")) or (len(text) > 1 and text[1] == ":"):
        raise ConfinementError(f"{label} must be relative to the allowed root")
    parts = [seg for seg in re.split(r"[\\/]+", text) if seg not in ("", ".")]
    if any(seg == ".." for seg in parts):
        raise ConfinementError(f"{label} must not traverse with '..'")
    if any(seg.split(".")[0].lower() in _RESERVED_DEVICE_NAMES for seg in parts):
        raise ConfinementError(f"{label} must not use a reserved device name")
    return parts


def resolve_within(root: Path, *relative_parts: str) -> Path:
    """Resolve a relative path inside `root`, or raise ConfinementError."""
    root = Path(root).resolve()
    for index, part in enumerate(relative_parts):
        _check_relative_segment(f"path component {index + 1}", part)
    target = root.joinpath(*[str(p) for p in relative_parts]).resolve()
    if target != root and root not in target.parents:
        raise ConfinementError("resolved path escapes the allowed root")
    return target


def confine_existing(candidate: str, root: Path, *, must_exist: bool = True) -> Path:
    """Resolve an absolute-or-relative path and require it under `root`.

    Unlike resolve_within, the caller here is naming a real directory they
    already have, so an absolute path is legitimate -- it just has to be inside
    the root once every symlink is resolved.
    """
    root = Path(root).resolve()
    text = str(candidate or "").strip().strip('"').strip("'")
    if not text:
        raise ConfinementError("path must not be empty")

    try:
        target = Path(text)
        target = (target if target.is_absolute() else root / target).resolve()
    except (OSError, ValueError) as exc:
        raise ConfinementError(f"path could not be resolved: {type(exc).__name__}") from None

    if target != root and root not in target.parents:
        raise ConfinementError(
            f"path is outside the allowed root. This agent may only work "
            f"inside {root}."
        )
    if must_exist and not target.exists():
        raise ConfinementError("path does not exist")
    if must_exist and not target.is_dir():
        raise ConfinementError("path is not a directory")
    return target
