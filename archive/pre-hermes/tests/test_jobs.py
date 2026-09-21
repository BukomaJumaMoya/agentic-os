#!/usr/bin/env python3
"""
Job records, checkpoints, cancellation and retention.

The clock is injected, never slept on. A timeout test that waited out a real
budget would be unrunnable, and one that shrank the budget to a fraction of a
second would be asserting against a timeout it had quietly redefined.
"""

import json
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _state_isolation  # noqa: E402,F401

from orchestrator import approval, jobs  # noqa: E402

passed = 0
_REAL_NOW = jobs._now
_APPROVAL_NOW = approval._now


def ok(name):
    global passed
    passed += 1
    print(f"PASS: {name}")


class _Clock:
    """Freeze and advance the clock for jobs and approval together."""

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
        approval._now = _APPROVAL_NOW
        return False


def setup():
    for d in (jobs.JOBS_DIR, approval.APPROVAL_DIR, approval.EVIDENCE_DIR):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True, exist_ok=True)


ENQUIRY = ("Brown Optical Limited requires a system to help them communicate "
           "with their clients, including appointment reminders sent across "
           "several messaging platforms.")


def test_record_never_contains_the_full_inputs():
    """An enquiry is client data; .approval already holds it in full."""
    setup()
    with _Clock():
        job = jobs.create("proposal", {"enquiry": ENQUIRY}, requested_by="123")
    raw = jobs.record_path(job["job_id"]).read_text(encoding="utf-8")
    assert ENQUIRY not in raw
    # A distinctive tail of the enquiry must not survive anywhere in the record.
    assert "several messaging platforms" not in raw
    assert job["inputs_preview"].startswith("Brown Optical")
    assert len(job["inputs_preview"]) <= jobs.PREVIEW_CHARS + 3
    assert job["inputs_digest"].startswith("sha256:")
    ok("record_never_contains_the_full_inputs")


def test_lifecycle_statuses():
    setup()
    with _Clock() as clock:
        job = jobs.create("proposal", {"enquiry": "x"})
        jid = job["job_id"]
        assert jobs.get(jid)["status"] == jobs.QUEUED
        jobs.start(jid, pid=4242)
        assert jobs.get(jid)["status"] == jobs.RUNNING
        assert jobs.get(jid)["pid"] == 4242          # recorded, never used to kill
        clock.advance(seconds=5)
        jobs.finish(jid, jobs.SUCCEEDED, result_ref={"request_id": "r-1"})
        rec = jobs.get(jid)
        assert rec["status"] == jobs.SUCCEEDED
        assert rec["finished_at"] is not None
    ok("lifecycle_statuses")


def test_cancellation_fires_at_every_checkpoint():
    """Each declared checkpoint must actually stop the work."""
    setup()
    steps = ["before:research", "after:research", "before:synthesis",
             "before:approval_request"]
    with _Clock():
        for step in steps:
            job = jobs.create("proposal", {"enquiry": "x"})
            jid = job["job_id"]
            jobs.start(jid)
            # Everything before the chosen checkpoint passes cleanly.
            for earlier in steps[:steps.index(step)]:
                jobs.checkpoint(jid, earlier)
            jobs.request_cancel(jid)
            try:
                jobs.checkpoint(jid, step)
            except jobs.Cancelled as e:
                assert e.step == step
            else:
                raise AssertionError(f"checkpoint {step} did not stop the job")
    ok("cancellation_fires_at_every_checkpoint")


def test_cancel_is_a_separate_file_and_never_edits_the_record():
    """Single writer each way: the record and the signal never collide."""
    setup()
    with _Clock():
        job = jobs.create("proposal", {"enquiry": "x"})
        jid = job["job_id"]
        jobs.start(jid)
        before = jobs.record_path(jid).read_text(encoding="utf-8")
        jobs.request_cancel(jid)
        after = jobs.record_path(jid).read_text(encoding="utf-8")
        assert before == after, "request_cancel modified the job record"
        assert jobs.cancel_path(jid).exists()
        assert jobs.cancel_requested(jid) is True
    ok("cancel_is_a_separate_file_and_never_edits_the_record")


def test_timeout_fires_at_a_checkpoint():
    setup()
    with _Clock() as clock:
        job = jobs.create("proposal", {"enquiry": "x"}, max_runtime_seconds=300)
        jid = job["job_id"]
        jobs.start(jid)
        clock.advance(seconds=120)
        jobs.checkpoint(jid, "before:research")      # inside the budget
        clock.advance(seconds=400)                   # 520s total, past 300s
        try:
            jobs.checkpoint(jid, "before:synthesis")
        except jobs.TimedOut as e:
            assert e.step == "before:synthesis"
            assert e.budget == 300
            assert e.elapsed > 300
        else:
            raise AssertionError("the budget was not enforced")
    ok("timeout_fires_at_a_checkpoint")


def test_cancel_after_consumption_is_too_late():
    """An executed external action cannot be unsent."""
    setup()
    with _Clock():
        approval.request_approval({"proposal_id": "r-tl", "body": "x"}, "e")
        approval.record_decision("r-tl", approved=True, approver="juma")
        job = jobs.create("proposal", {"enquiry": "x"})
        jid = job["job_id"]
        jobs.start(jid)
        jobs.attach_result_ref(jid, {"request_id": "r-tl", "artifact": "proposal"})

        # Still stoppable while the approval is unused.
        assert jobs.request_cancel(jid)["outcome"] == "cancelling"
        jobs.cancel_path(jid).unlink()

        approval.consume_decision("r-tl")            # the action happened
        out = jobs.request_cancel(jid)
        assert out["ok"] is False
        assert out["outcome"] == "too_late_already_executed", out
        assert not jobs.cancel_path(jid).exists()
    ok("cancel_after_consumption_is_too_late")


def test_cancel_outcomes_are_distinct():
    setup()
    with _Clock():
        assert jobs.request_cancel("no-such-job")["outcome"] == "not_found"
        job = jobs.create("proposal", {"enquiry": "x"})
        jid = job["job_id"]
        jobs.finish(jid, jobs.SUCCEEDED)
        out = jobs.request_cancel(jid)
        assert out["outcome"] == "already_finished", out
    ok("cancel_outcomes_are_distinct")


def test_checkpoint_without_a_job_is_a_no_op():
    """Workflows run outside the job runner behave as they did before jobs."""
    setup()
    jobs.checkpoint(None, "before:research")
    jobs.checkpoint("", "before:synthesis")
    ok("checkpoint_without_a_job_is_a_no_op")


def test_progress_is_recorded_and_pulled_not_pushed():
    setup()
    with _Clock() as clock:
        job = jobs.create("proposal", {"enquiry": "x"})
        jid = job["job_id"]
        jobs.start(jid)
        jobs.checkpoint(jid, "before:research")
        clock.advance(seconds=3)
        jobs.checkpoint(jid, "after:research", "12 sources")
        listed = jobs.list_jobs()
        entry = next(j for j in listed if j["job_id"] == jid)
        assert entry["last_step"] == "after:research"
        assert entry["last_message"] == "12 sources"
        # A listing carries identifiers and progress, never the artifact.
        assert "proposal" not in json.dumps(entry).replace('"workflow": "proposal"', "")
    ok("progress_is_recorded_and_pulled_not_pushed")


def test_retention_sweeps_orphans_and_expiry():
    setup()
    with _Clock() as clock:
        # 1. Tied to the approval request: request gone -> job goes.
        approval.request_approval({"proposal_id": "r-keep"}, "e")
        linked = jobs.create("proposal", {"enquiry": "x"})
        jobs.attach_result_ref(linked["job_id"],
                               {"request_id": "r-keep", "artifact": "proposal"})
        jobs.finish(linked["job_id"], jobs.SUCCEEDED,
                    result_ref={"request_id": "r-keep", "artifact": "proposal"})

        orphan = jobs.create("proposal", {"enquiry": "y"})
        jobs.attach_result_ref(orphan["job_id"],
                               {"request_id": "r-gone", "artifact": "proposal"})
        jobs.finish(orphan["job_id"], jobs.SUCCEEDED,
                    result_ref={"request_id": "r-gone", "artifact": "proposal"})

        removed = jobs.sweep()
        assert orphan["job_id"] in removed["orphaned"], removed
        assert jobs.get(linked["job_id"]) is not None, "a live request lost its job"

        # 2. The 30-day ceiling applies even with no request at all.
        old = jobs.create("proposal", {"enquiry": "z"})
        jobs.finish(old["job_id"], jobs.FAILED, error="nope")
        clock.advance(days=31)
        removed = jobs.sweep()
        assert old["job_id"] in removed["expired"], removed

        # 3. A running job is never swept, however old.
        running = jobs.create("proposal", {"enquiry": "w"})
        jobs.start(running["job_id"])
        clock.advance(days=90)
        jobs.sweep()
        assert jobs.get(running["job_id"]) is not None, "swept work in flight"
    ok("retention_sweeps_orphans_and_expiry")


def test_job_runner_cannot_send_when_outbound_is_blocked():
    """A job spawned from a test must be structurally incapable of sending."""
    setup()
    with _Clock():
        job = jobs.create("proposal", {"enquiry": ""}, requested_by="1360833951")
    payload = json.dumps({"job_id": job["job_id"], "workflow": "proposal",
                          "inputs": {"enquiry": ""}, "user_id": "1360833951"})
    out = subprocess.run([sys.executable, "orchestrator/job_runner.py"],
                         input=payload, capture_output=True, text=True,
                         cwd=str(BASE), timeout=180)
    # The runner notifies on failure; that notification must be refused.
    notified = [json.loads(line) for line in out.stdout.splitlines()
                if line.startswith("{") and "notify" in line]
    assert notified, out.stdout + out.stderr
    assert all(n.get("delivery_status") == "blocked" for n in notified), notified
    assert jobs.get(job["job_id"])["status"] == jobs.FAILED
    ok("job_runner_cannot_send_when_outbound_is_blocked")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    try:
        for t in tests:
            t()
        print(f"\nALL JOB TESTS PASSED ({passed})")
        return 0
    except Exception as e:
        print(f"FAIL: {e}")
        import traceback
        traceback.print_exc()
        return 1
    finally:
        jobs._now = _REAL_NOW
        approval._now = _APPROVAL_NOW
        for d in (jobs.JOBS_DIR, approval.APPROVAL_DIR, approval.EVIDENCE_DIR):
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
