#!/usr/bin/env python3
"""
Structural guards: tests must not be able to send, or to touch live state.

Both guards exist because both failures actually happened in this repository.

Six identical proposal PDFs reached the operator's phone from a test sweep. The
suites had mocked the one sending function they knew about; document delivery
was added later and nobody mocked it. Mocking is a convention, and conventions
do not survive new code paths.

Twice, a suite destroyed the real .approval/ and evidence/ directories -- audit
F-23 -- because it computed those paths from BASE independently of the isolation
module it had already imported.

These tests assert the guards themselves, and that every suite here obeys them.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _state_isolation  # noqa: E402,F401

from orchestrator import telegram_approval as tg  # noqa: E402

passed = 0


def ok(name):
    global passed
    passed += 1
    print(f"PASS: {name}")


def test_importing_state_isolation_blocks_outbound():
    """Isolating state and blocking sends are the same requirement."""
    assert os.environ.get("AGENTIC_BLOCK_OUTBOUND") == "1"
    assert tg.outbound_blocked() is True
    ok("importing_state_isolation_blocks_outbound")


def test_direct_message_is_refused():
    r = tg.send_direct_message("1360833951", "this must never arrive")
    assert r["sent"] is False
    assert r["delivery_status"] == "blocked", r
    ok("direct_message_is_refused")


def test_document_delivery_is_refused():
    r = tg.send_document("1360833951", BASE / "requirements.txt", caption="nope")
    assert r["delivered"] is False
    assert r["document_status"] == "blocked", r
    ok("document_delivery_is_refused")


def test_gateway_tool_calls_are_refused():
    """Blocked before any socket, so conversations_send cannot be reached."""
    r = tg._invoke_tool("conversations_send", {"conversationRef": "x", "message": "y"})
    assert r["ok"] is False
    assert "outbound blocked" in str(r["error"])
    assert r["http_status"] == 0
    ok("gateway_tool_calls_are_refused")


def test_the_guard_is_off_by_default_in_a_clean_process():
    """Production must be unaffected: the block is opt-in, set only by tests."""
    code = (
        "import sys; sys.path.insert(0, r'%s');"
        "from orchestrator import telegram_approval as t;"
        "print(t.outbound_blocked())" % str(BASE)
    )
    env = dict(os.environ)
    env.pop("AGENTIC_BLOCK_OUTBOUND", None)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, env=env, cwd=str(BASE))
    assert out.stdout.strip() == "False", out.stdout + out.stderr
    ok("the_guard_is_off_by_default_in_a_clean_process")


def test_subprocesses_inherit_the_block():
    """Agents are child processes; a flag they do not inherit protects nothing."""
    code = (
        "import sys; sys.path.insert(0, r'%s');"
        "from orchestrator import telegram_approval as t;"
        "print(t.outbound_blocked())" % str(BASE)
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, cwd=str(BASE))
    assert out.stdout.strip() == "True", out.stdout + out.stderr
    ok("subprocesses_inherit_the_block")


def test_every_suite_here_blocks_outbound():
    """No suite may reach a send path without the guard on.

    Checked by source inspection rather than by running them: a suite reaching a
    transport is the thing being prevented.
    """
    tests_dir = Path(__file__).resolve().parent
    markers = ("run_workflow", "execute_external_action", "send_document",
               "send_direct_message", "telegram_approval")
    unguarded = []
    for path in sorted(tests_dir.glob("test_*.py")):
        text = path.read_text(encoding="utf-8")
        if not any(m in text for m in markers):
            continue
        if "_state_isolation" not in text and "_no_outbound" not in text:
            unguarded.append(path.name)
    assert not unguarded, (
        "these suites can reach a send path without the outbound block: "
        + ", ".join(unguarded))
    ok("every_suite_here_blocks_outbound")


def test_no_suite_points_at_the_real_state_directories():
    """No test may build a path into the repository's live state.

    A suite that computes BASE / ".approval" and then rmtree's it destroys real
    approval requests and proposal evidence. Twice now a suite looked isolated
    because it imported _state_isolation, while its directory constants were
    assigned from BASE independently of it. The constants must come FROM the
    isolation module, never be rebuilt alongside it.
    """
    tests_dir = Path(__file__).resolve().parent
    pattern = re.compile(r"BASE\s*/\s*[\"'][.]?(?:approval|evidence)[\"']")
    offenders = []
    this_file = Path(__file__).name
    for path in sorted(tests_dir.glob("test_*.py")):
        # Skip this file: its own documentation quotes the pattern it forbids.
        if path.name == this_file:
            continue
        for num, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{path.name}:{num}")
    assert not offenders, (
        "suites building paths into the real state directories: "
        + ", ".join(offenders))
    ok("no_suite_points_at_the_real_state_directories")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    try:
        for t in tests:
            t()
        print(f"\nALL STRUCTURAL GUARD TESTS PASSED ({passed})")
        return 0
    except Exception as e:
        print(f"FAIL: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
