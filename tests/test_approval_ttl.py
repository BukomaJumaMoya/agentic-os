#!/usr/bin/env python3
"""
Audit F-5 — approvals expire, and are consumed on use.

The clock is injected, never slept on. approval._now is a function precisely so
these tests can move time; a suite that waited fifteen real minutes to prove an
expiry would be untestable, and one that slept a fraction of a second would be
asserting against a TTL it had quietly shrunk to nothing.
"""

import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _state_isolation  # noqa: E402,F401 -- MUST precede orchestrator imports

APPROVAL_DIR = _state_isolation.APPROVAL_DIR
EVIDENCE_DIR = _state_isolation.EVIDENCE_DIR

from orchestrator import approval  # noqa: E402
from orchestrator.approval import (  # noqa: E402
    request_approval, record_decision, is_approved, resume_if_approved,
    decision_state, consume_decision, cleanup,
)

passed = 0
_REAL_NOW = approval._now


def ok(name):
    global passed
    passed += 1
    print(f"PASS: {name}")


class _Clock:
    """Freeze and advance the module clock."""

    def __init__(self, start=None):
        self.t = start or datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    def __enter__(self):
        approval._now = lambda: self.t
        return self

    def advance(self, **kw):
        self.t = self.t + timedelta(**kw)

    def __exit__(self, *exc):
        approval._now = _REAL_NOW
        return False


def setup():
    for d in (APPROVAL_DIR, EVIDENCE_DIR):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True, exist_ok=True)


def test_fresh_approval_is_usable():
    setup()
    with _Clock() as clock:
        request_approval({"proposal_id": "f-1", "body": "x"}, "enquiry")
        record_decision("f-1", approved=True, approver="juma")
        clock.advance(minutes=5)          # well inside the 15-minute TTL
        assert is_approved("f-1") is True
        r = resume_if_approved("f-1")
        assert r["approved"] is True
        assert r["state"] == "approved"
        assert r["seconds_remaining"] > 0
    ok("fresh_approval_is_usable")


def test_expired_approval_is_refused():
    setup()
    with _Clock() as clock:
        request_approval({"proposal_id": "f-2", "body": "x"}, "enquiry")
        record_decision("f-2", approved=True, approver="juma")
        clock.advance(minutes=16)         # past the 15-minute default
        assert is_approved("f-2") is False
        r = resume_if_approved("f-2")
        assert r["approved"] is False
        # Distinct from pending and from rejected.
        assert r["reason"] == "expired", r
        assert r["state"] == "expired"
        assert r["expired_for_seconds"] > 0
    ok("expired_approval_is_refused")


def test_consumed_approval_is_refused_on_second_use():
    setup()
    with _Clock():
        request_approval({"proposal_id": "f-3", "body": "x"}, "enquiry")
        record_decision("f-3", approved=True, approver="juma")
        assert resume_if_approved("f-3")["approved"] is True

        first = consume_decision("f-3")
        assert first["consumed"] is True

        # The same approval must not authorise a second action.
        assert is_approved("f-3") is False
        r = resume_if_approved("f-3")
        assert r["approved"] is False
        assert r["reason"] == "already_used", r
        assert r["state"] == "consumed"

        second = consume_decision("f-3")
        assert second["consumed"] is False
        assert second["reason"] == "already_consumed"
    ok("consumed_approval_is_refused_on_second_use")


def test_consumption_is_an_atomic_rename():
    setup()
    with _Clock():
        request_approval({"proposal_id": "f-4", "body": "x"}, "enquiry")
        record_decision("f-4", approved=True, approver="juma")
        assert approval.decision_path("f-4").exists()
        consume_decision("f-4")
        # The decision file is MOVED, not copied: no window in which both exist
        # and a second caller could read the original as still valid.
        assert not approval.decision_path("f-4").exists()
        assert approval.consumed_path("f-4").exists()
    ok("consumption_is_an_atomic_rename")


def test_four_states_are_distinguishable():
    setup()
    with _Clock() as clock:
        assert decision_state("none")["state"] == "pending"

        request_approval({"proposal_id": "f-5a"}, "e")
        record_decision("f-5a", approved=False, approver="juma", reason="no")
        assert decision_state("f-5a")["state"] == "rejected"

        request_approval({"proposal_id": "f-5b"}, "e")
        record_decision("f-5b", approved=True, approver="juma")
        assert decision_state("f-5b")["state"] == "approved"

        consume_decision("f-5b")
        assert decision_state("f-5b")["state"] == "consumed"

        request_approval({"proposal_id": "f-5c"}, "e")
        record_decision("f-5c", approved=True, approver="juma")
        clock.advance(hours=1)
        assert decision_state("f-5c")["state"] == "expired"
    ok("four_states_are_distinguishable")


def test_rejection_does_not_expire_into_permission():
    setup()
    with _Clock() as clock:
        request_approval({"proposal_id": "f-6"}, "e")
        record_decision("f-6", approved=False, approver="juma", reason="not this one")
        clock.advance(days=30)
        state = decision_state("f-6")
        assert state["state"] == "rejected", state
        assert is_approved("f-6") is False
    ok("rejection_does_not_expire_into_permission")


def test_decision_without_a_deadline_still_ages_out():
    """A decision written before TTLs existed must not be immortal."""
    setup()
    with _Clock() as clock:
        request_approval({"proposal_id": "f-7"}, "e")
        record_decision("f-7", approved=True, approver="juma")
        # Strip the deadline, as an older record would have been.
        import json
        path = approval.decision_path("f-7")
        old = json.loads(path.read_text())
        old.pop("expires_at", None)
        old.pop("ttl_seconds", None)
        path.write_text(json.dumps(old))

        assert decision_state("f-7")["state"] == "approved"
        clock.advance(minutes=20)
        assert decision_state("f-7")["state"] == "expired"
    ok("decision_without_a_deadline_still_ages_out")


def test_ttl_is_configurable():
    setup()
    import os
    saved = os.environ.get("APPROVAL_DECISION_TTL_SECONDS")
    os.environ["APPROVAL_DECISION_TTL_SECONDS"] = "60"
    try:
        with _Clock() as clock:
            assert approval.decision_ttl_seconds() == 60
            request_approval({"proposal_id": "f-8"}, "e")
            record_decision("f-8", approved=True, approver="juma")
            clock.advance(seconds=30)
            assert is_approved("f-8") is True
            clock.advance(seconds=45)     # 75s total, past the 60s TTL
            assert is_approved("f-8") is False
    finally:
        os.environ.pop("APPROVAL_DECISION_TTL_SECONDS", None)
        if saved is not None:
            os.environ["APPROVAL_DECISION_TTL_SECONDS"] = saved
    ok("ttl_is_configurable")


def test_approving_a_stale_request_is_refused():
    setup()
    with _Clock() as clock:
        request_approval({"proposal_id": "f-9"}, "e")
        clock.advance(days=2)             # past the 24h request TTL
        decision = record_decision("f-9", approved=True, approver="juma")
        assert decision["approved"] is False
        assert decision["reason"] == "request_expired"
        assert decision.get("recorded") is False
        # Nothing was written, so the request stays pending rather than
        # silently becoming approved.
        assert decision_state("f-9")["state"] == "pending"
    ok("approving_a_stale_request_is_refused")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    try:
        for t in tests:
            t()
        print(f"\nALL APPROVAL TTL TESTS PASSED ({passed})")
        return 0
    except Exception as e:
        print(f"FAIL: {e}")
        import traceback
        traceback.print_exc()
        return 1
    finally:
        approval._now = _REAL_NOW
        for d in (APPROVAL_DIR, EVIDENCE_DIR):
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
