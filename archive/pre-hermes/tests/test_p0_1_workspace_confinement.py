#!/usr/bin/env python3
"""
P0-1 regression: workspace confinement for coding-agent writes.

Audit F-11 proved arbitrary filesystem write via unvalidated `output_dir` /
`filename`, which was the root of the RCE chain. `Path(a) / b` discards `a`
entirely when `b` is absolute, so an absolute filename wrote anywhere the
process could write.

This is a parametrised traversal suite including the Windows-specific forms
the audit called for: drive letters, UNC prefixes, backslash separators and
reserved device names.
"""

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "agents" / "coding"))

from main import resolve_write_target, WORKSPACE_ROOT  # noqa: E402


# (output_dir, filename) pairs that must all be refused.
ESCAPES = [
    # Absolute POSIX
    (".", "/etc/passwd"),
    ("/tmp", "evil.txt"),
    # Absolute Windows, both separators
    (".", r"C:\Users\HP\.openclaw\openclaw.json"),
    (".", "C:/Users/HP/.openclaw/openclaw.json"),
    (r"C:\Windows\Temp", "evil.txt"),
    # UNC
    (".", r"\\server\share\evil.txt"),
    ("//server/share", "evil.txt"),
    # Traversal, both separators, in either field
    (".", "../../../etc/passwd"),
    (".", r"..\..\..\openclaw.json"),
    ("../..", "evil.txt"),
    ("subdir/../../..", "evil.txt"),
    # The exact payload from audit F-11
    ("/home/claude/victim", "../victim/config.json"),
    # Reserved Windows device names
    (".", "CON"),
    (".", "nul.txt"),
    (".", "LPT1.log"),
    ("aux", "evil.txt"),
]

# Pairs that are legitimate and must be allowed.
ALLOWED = [
    (".", "generated_code.txt"),
    ("out", "module.py"),
    ("out/nested/deep", "module.py"),
    (r"out\nested", "module.py"),
    (None, None),
    # Empty filename legitimately falls back to the default name.
    (".", ""),
]


def test_escapes_refused():
    failures = []
    for output_dir, filename in ESCAPES:
        try:
            target = resolve_write_target(output_dir, filename)
        except ValueError:
            continue
        failures.append(f"({output_dir!r}, {filename!r}) -> {target}")
    assert not failures, "escaped the workspace:\n  " + "\n  ".join(failures)
    print(f"PASS: escapes_refused ({len(ESCAPES)} cases)")


def test_allowed_paths_resolve_inside_workspace():
    for output_dir, filename in ALLOWED:
        target = resolve_write_target(output_dir, filename)
        assert WORKSPACE_ROOT in target.parents, f"{target} is outside {WORKSPACE_ROOT}"
    print(f"PASS: allowed_paths_resolve_inside_workspace ({len(ALLOWED)} cases)")


def test_absolute_filename_does_not_discard_output_dir():
    """Path('a') / '/abs' == '/abs' -- the specific bug behind F-11."""
    try:
        resolve_write_target("out", "/etc/passwd")
    except ValueError:
        print("PASS: absolute_filename_does_not_discard_output_dir")
        return
    raise AssertionError("absolute filename was not refused")


if __name__ == "__main__":
    try:
        test_escapes_refused()
        test_allowed_paths_resolve_inside_workspace()
        test_absolute_filename_does_not_discard_output_dir()
        print("\nALL P0-1 TESTS PASSED")
    except AssertionError as e:
        print(f"FAIL: {e}")
        sys.exit(1)
