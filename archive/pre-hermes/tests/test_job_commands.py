#!/usr/bin/env python3
"""
JOBS / CANCEL / ENQUIRY command dispatch.

Exercises the Python bridge the slash commands call, including short-id prefix
resolution and the rule that a listing never carries inputs or artifacts.

The spawner is stubbed: these tests assert dispatch, not that a detached process
starts. A real spawn is covered by test_jobs.test_job_runner_cannot_send_when_
outbound_is_blocked.
"""

import json
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _state_isolation  # noqa: E402,F401

from orchestrator import approval, jobs  # noqa: E402
from orchestrator.telegram_commands import handle_telegram_command  # noqa: E402

passed = 0
_REAL_NOW = jobs._now
_REAL_SPAWN = jobs.spawn
USER = None


def ok(name):
    global passed
    passed += 1
    print(f"PASS: {name}")


class _Clock:
    def __init__(self, start=None):
        self.t = start or datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    def __enter__(self):
        jobs._now = lambda: self.t
        approval._now = lambda: self.t
        return self

    def advance(self, **kw):
        self.t = self.t + timedelta(**kw)

    def __exit__(self, *exc):
        jobs._now = _REAL_NOW
        return False


SPAWNED = []


def _fake_spawn(job_id, workflow, inputs, user_id=None):
    SPAWNED.append({"job_id": job_id, "workflow": workflow, "inputs": inputs})
    return {"spawned": True, "pid": 4242}


def setup():
    global USER
    USER = approval.allowed_user_id()
    SPAWNED.clear()
    jobs.spawn = _fake_spawn
    for d in (jobs.JOBS_DIR, approval.APPROVAL_DIR, approval.EVIDENCE_DIR):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True, exist_ok=True)


def cmd(text):
    return handle_telegram_command(USER, text)


ENQUIRY = ("Brown Optical Limited requires a system to help them communicate "
           "with their clients across several messaging platforms.")


def test_unauthorised_sender_is_refused():
    setup()
    out = handle_telegram_command("999999", "JOBS")
    assert out["ok"] is False
    assert out["error"] == "not_allowed"
    ok("unauthorised_sender_is_refused")


def test_enquiry_creates_a_job_and_spawns_the_runner():
    setup()
    with _Clock():
        out = cmd(f"ENQUIRY {ENQUIRY}")
    assert out["ok"] is True, out
    assert out["status"] == "queued"
    assert len(out["short_id"]) == 8
    assert SPAWNED and SPAWNED[0]["workflow"] == "proposal"
    # The job exists BEFORE the reply, so /apr_jobs shows it immediately.
    assert jobs.get(out["job_id"])["status"] == jobs.QUEUED
    ok("enquiry_creates_a_job_and_spawns_the_runner")


def test_enquiry_refuses_rather_than_truncates():
    setup()
    out = cmd("ENQUIRY " + "x" * 8001)
    assert out["ok"] is False
    assert out["error"] == "enquiry_too_long"
    assert out["length"] == 8001
    assert not SPAWNED, "a job was started for an over-long enquiry"
    ok("enquiry_refuses_rather_than_truncates")


def test_enquiry_without_text_is_refused():
    setup()
    out = cmd("ENQUIRY")
    assert out["ok"] is False
    assert out["error"] == "missing_enquiry"
    ok("enquiry_without_text_is_refused")


def test_jobs_listing_never_emits_inputs_or_bodies():
    """Same discipline as /apr_list: identifiers and progress, nothing else."""
    setup()
    with _Clock():
        cmd(f"ENQUIRY {ENQUIRY}")
        job_id = SPAWNED[0]["job_id"]
        jobs.start(job_id)
        jobs.checkpoint(job_id, "before:research", "4 queries")
        out = cmd("JOBS")

    blob = json.dumps(out)
    # Nothing of the enquiry, not even the preview, reaches the listing.
    assert "Brown Optical" not in blob, blob
    assert "messaging platforms" not in blob
    for forbidden in ("body", "proposal_body", "evidence", "inputs",
                      "preview", "raw_text", "gathered_context"):
        assert forbidden not in blob, f"{forbidden} leaked into /apr_jobs"
    entry = out["jobs"][0]
    assert set(entry) <= {"job", "workflow", "status", "age", "at",
                          "cancelling", "request"}
    assert entry["at"] == "before:research"
    ok("jobs_listing_never_emits_inputs_or_bodies")


def test_jobs_listing_is_capped():
    setup()
    with _Clock() as clock:
        for _ in range(14):
            jobs.create("proposal", {"enquiry": "x"})
            clock.advance(seconds=1)
        out = cmd("JOBS")
    assert out["shown"] == 10, out["shown"]
    assert out["limit"] == 10
    # An explicit smaller limit is honoured; a larger one is clamped.
    assert cmd("JOBS 3")["shown"] == 3
    assert cmd("JOBS 99")["shown"] == 10
    ok("jobs_listing_is_capped")


def test_cancel_accepts_an_unambiguous_prefix():
    setup()
    with _Clock():
        cmd(f"ENQUIRY {ENQUIRY}")
        job_id = SPAWNED[0]["job_id"]
        jobs.start(job_id)
        out = cmd(f"CANCEL {job_id[:8]}")
    assert out["ok"] is True, out
    assert out["outcome"] == "cancelling"
    assert jobs.cancel_requested(job_id)
    ok("cancel_accepts_an_unambiguous_prefix")


def test_cancel_refuses_an_ambiguous_prefix():
    """Guessing which job someone meant to stop is the wrong instinct."""
    setup()
    with _Clock():
        a = jobs.create("proposal", {"enquiry": "a"})
        b = jobs.create("proposal", {"enquiry": "b"})
        # Force a shared prefix by renaming the records.
        for rec, new_id in ((a, "dupe1111-aaaa"), (b, "dupe2222-bbbb")):
            jobs.record_path(rec["job_id"]).unlink()
            rec["job_id"] = new_id
            jobs._write(rec)
            jobs.start(new_id)
        out = cmd("CANCEL dupe")
    assert out["ok"] is False
    assert out["outcome"] == "ambiguous_job_id", out
    assert len(out["matches"]) == 2
    assert not jobs.cancel_requested("dupe1111-aaaa")
    assert not jobs.cancel_requested("dupe2222-bbbb")
    ok("cancel_refuses_an_ambiguous_prefix")


def test_cancel_not_found():
    setup()
    out = cmd("CANCEL zzzzzzzz")
    assert out["ok"] is False
    assert out["outcome"] == "not_found", out
    ok("cancel_not_found")


def test_cancel_already_finished():
    setup()
    with _Clock():
        job = jobs.create("proposal", {"enquiry": "x"})
        jobs.finish(job["job_id"], jobs.SUCCEEDED)
        out = cmd(f"CANCEL {job['job_id'][:8]}")
    assert out["ok"] is False
    assert out["outcome"] == "already_finished", out
    ok("cancel_already_finished")


def test_cancel_too_late_after_consumption():
    setup()
    with _Clock():
        approval.request_approval({"proposal_id": "r-late", "body": "x"}, "e")
        approval.record_decision("r-late", approved=True, approver="juma")
        job = jobs.create("proposal", {"enquiry": "x"})
        jobs.start(job["job_id"])
        jobs.attach_result_ref(job["job_id"],
                               {"request_id": "r-late", "artifact": "proposal"})
        approval.consume_decision("r-late")
        out = cmd(f"CANCEL {job['job_id'][:8]}")
    assert out["ok"] is False
    assert out["outcome"] == "too_late_already_executed", out
    ok("cancel_too_late_after_consumption")


def test_cancel_without_an_id_is_refused():
    setup()
    out = cmd("CANCEL")
    assert out["ok"] is False
    assert out["error"] == "missing_job_id"
    ok("cancel_without_an_id_is_refused")


def test_dry_run_spends_nothing():
    """Answers "what would this do" without an agent, a model call or a send."""
    setup()
    from orchestrator import workflow, flagship

    called = []
    real = flagship._exec_with_retry
    flagship._exec_with_retry = lambda *a, **k: called.append(a) or ({}, None)
    try:
        out = workflow.run("proposal", {"enquiry": "x", "bogus": 1}, dry_run=True)
    finally:
        flagship._exec_with_retry = real

    assert out["status"] == "dry_run"
    assert out["would_invoke"] == ["research", "projects", "coding"]
    assert out["would_produce"] == "proposal"
    assert out["requires_approval"] is True
    assert out["inputs_accepted"] == ["enquiry"]
    assert out["inputs_ignored"] == ["bogus"]
    # Nothing ran: no agent, and therefore no model call and no approval request.
    assert called == [], "a dry run invoked an agent"
    assert not list(approval.APPROVAL_DIR.glob("*.request.json"))
    ok("dry_run_spends_nothing")


def test_dry_run_still_validates_inputs():
    """A bad request is rejected before the preview, not previewed as valid."""
    setup()
    from orchestrator import workflow
    out = workflow.run("proposal", {"enquiry": 123}, dry_run=True)
    assert out["status"] == "invalid_input", out
    ok("dry_run_still_validates_inputs")


def test_unknown_verb_lists_the_supported_set():
    setup()
    out = cmd("FLY")
    assert out["ok"] is False
    assert out["error"] == "unknown_command"
    assert set(out["supported"]) >= {"JOBS", "CANCEL", "ENQUIRY"}
    ok("unknown_verb_lists_the_supported_set")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    try:
        for t in tests:
            t()
        print(f"\nALL JOB COMMAND TESTS PASSED ({passed})")
        return 0
    except Exception as e:
        print(f"FAIL: {e}")
        import traceback
        traceback.print_exc()
        return 1
    finally:
        jobs._now = _REAL_NOW
        jobs.spawn = _REAL_SPAWN
        approval._now = getattr(approval, "_now", None) or approval._now
        for d in (jobs.JOBS_DIR, approval.APPROVAL_DIR, approval.EVIDENCE_DIR):
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
