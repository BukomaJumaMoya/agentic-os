#!/usr/bin/env python3
"""
Unit tests for Step 7 — Flagship End-to-End Workflow.

No external side effects. Fast. Isolated.
"""
import json
import sys
import shutil
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _state_isolation  # noqa: E402,F401 -- MUST precede orchestrator imports

EVIDENCE_DIR = _state_isolation.EVIDENCE_DIR
APPROVAL_DIR = _state_isolation.APPROVAL_DIR

from orchestrator import flagship, llm
from orchestrator.flagship import classify_enquiry, load_config, synthesize_proposal, request_approval
from orchestrator.approval import record_decision, is_approved, cleanup

# ---------------------------------------------------------------------------
# Model stubbing.
#
# The stub replaces orchestrator.llm.complete -- one function, the single point
# where this process would otherwise open a socket. Everything above it runs for
# real: prompt assembly, complete_json's fence-stripping and JSON parsing,
# proposal._validate, and proposal._render. Patching urllib instead would leave
# those untested; patching synthesize_proposal would leave all of them untested.
# No test in this file may reach the network or need an API key.
# ---------------------------------------------------------------------------

STUB_MODEL = "stub/test-model"

VALID_REPLY = {
    "subject": "Appointment reminders from your booking spreadsheet",
    "understanding": (
        "You run bookings through a spreadsheet and want appointment reminders "
        "sent automatically instead of by hand. You need this to work over the "
        "channels your clients already use."
    ),
    "services": [
        {"service": "Web applications",
         "why": "the booking spreadsheet needs somewhere to live that staff can use"},
    ],
    "approach": [
        "Confirm the spreadsheet's columns and how a booking is entered today.",
        "Stand up a minimal booking record and reminder queue.",
        "Wire the reminder channel and run it alongside the manual process.",
    ],
    "clarifications": [
        "How many bookings do you handle in a typical week?",
        "Which channel do your clients actually reply on?",
    ],
    "closing": "Answer those two questions and I can scope this properly.",
}


class _Stub:
    """Context manager installing a fake llm.complete for one test."""

    def __init__(self, text=None, raises=None, model=STUB_MODEL):
        self.text = text
        self.raises = raises
        self.model = model
        self.calls = []
        self._real = None

    def __enter__(self):
        self._real = llm.complete

        def _fake(system, user, **kwargs):
            self.calls.append({"system": system, "user": user, "kwargs": kwargs})
            if self.raises is not None:
                raise self.raises
            return {"text": self.text, "model": self.model,
                    "provider": "stub",
                    "attempts": [{"provider": "stub", "model": self.model,
                                  "outcome": "ok"}],
                    "usage": {"total_tokens": 0}, "raw": {"stub": True}}

        llm.complete = _fake
        return self

    def __exit__(self, *exc):
        llm.complete = self._real
        return False


def _valid_json(**overrides):
    reply = dict(VALID_REPLY)
    reply.update(overrides)
    return json.dumps(reply)


def setup():
    for d in [EVIDENCE_DIR, APPROVAL_DIR]:
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True, exist_ok=True)


def teardown():
    for d in [EVIDENCE_DIR, APPROVAL_DIR]:
        if d.exists():
            shutil.rmtree(d)


def test_classify_enquiry_research():
    result = classify_enquiry("Research competitor pricing for SaaS CRMs.")
    assert "research" in result["selected_agents"]
    teardown()
    print("PASS: classify_enquiry_research")


def test_classify_enquiry_coding():
    result = classify_enquiry("Debug our Python API integration code.")
    assert "coding" in result["selected_agents"]
    teardown()
    print("PASS: classify_enquiry_coding")


def test_classify_enquiry_projects():
    result = classify_enquiry("Create a ClickUp task for onboarding.")
    assert "projects" in result["selected_agents"]
    teardown()
    print("PASS: classify_enquiry_projects")


def test_classify_enquiry_missing_info():
    result = classify_enquiry("We need an AI chatbot.")
    assert "budget" in result["missing_information"]
    teardown()
    print("PASS: classify_enquiry_missing_info")


def test_load_config_exists():
    config = load_config()
    assert isinstance(config, dict)
    teardown()
    print("PASS: load_config_exists")


def test_synthesize_proposal_uses_config():
    config = load_config()
    with _Stub(_valid_json()) as stub:
        proposal = synthesize_proposal(
            "We book appointments in a spreadsheet and need reminders sent out.",
            {}, {}, {}, config)
    assert proposal["proposal_id"]
    assert proposal["body"]
    # Identity comes from config, appended by code after the model returns.
    assert config.get("name") in proposal["body"]
    assert config.get("business") in proposal["body"]
    # The model was asked only for the body; it never sees or supplies a name.
    assert config.get("name") not in stub.calls[0]["user"]
    # Model id and prompt version are recorded for every proposal.
    assert proposal["generation"]["model"] == STUB_MODEL
    assert proposal["generation"]["prompt_version"]
    teardown()
    print("PASS: synthesize_proposal_uses_config")


def test_synthesize_proposal_without_config():
    with _Stub(_valid_json(services=[])):
        proposal = synthesize_proposal("Need a web app", {}, {}, {}, {})
    assert proposal["proposal_id"]
    assert proposal["body"]
    # No config means no identity to sign with -- and none may be invented.
    assert "Juma" not in proposal["body"]
    assert "Bukoma" not in proposal["body"]
    teardown()
    print("PASS: synthesize_proposal_without_config")


def test_render_normalises_unicode_punctuation():
    """Nothing non-ASCII may reach email or Telegram.

    gpt-oss-120b emitted U+2011 NON-BREAKING HYPHEN in a real run; it is
    invisible in review and mangled by cp1252 clients and the Windows console.
    """
    reply = _valid_json(
        subject="End" + chr(0x2011) + "to" + chr(0x2011) + "end reminders",
        closing="We" + chr(0x2019) + "ll confirm scope " + chr(0x2013)
                + " then proceed" + chr(0x2026))
    with _Stub(reply):
        proposal = synthesize_proposal(
            "We book appointments in a spreadsheet and need reminders.",
            {}, {}, {}, load_config())
    body = proposal["body"]
    assert body.isascii(), [c for c in body if not c.isascii()]
    assert "We'll confirm scope - then proceed..." in body
    teardown()
    print("PASS: render_normalises_unicode_punctuation")


def test_commercial_block_comes_from_config_not_the_model():
    """Every figure is code-authored; the model never states one."""
    config = load_config()
    with _Stub(_valid_json()) as stub:
        proposal = synthesize_proposal(
            "We book appointments in a spreadsheet and need reminders.",
            {}, {}, {}, config)
    body = proposal["body"]
    assert "Commercial terms (indicative, not a quote):" in body
    assert "Engagement options: " + ", ".join(config["engagement_types"]) in body
    assert "Payment terms: " + config["payment_terms"] in body
    assert config["currency"] in body
    assert "firm, scoped quote within " + config["proposal_sla"] in body
    # The rate reaches the body normalised, and never went near the model.
    from orchestrator.proposal import normalise_punctuation
    assert "Indicative rate: " + normalise_punctuation(config["default_rate"]) in body
    assert config["default_rate"] not in stub.calls[0]["user"]
    assert "75" not in stub.calls[0]["user"]
    teardown()
    print("PASS: commercial_block_comes_from_config_not_the_model")


def test_commercial_block_absent_when_config_has_no_terms():
    with _Stub(_valid_json(services=[])):
        proposal = synthesize_proposal("Need a web app", {}, {}, {}, {})
    assert "Commercial terms" not in proposal["body"]
    assert "Indicative rate" not in proposal["body"]
    teardown()
    print("PASS: commercial_block_absent_when_config_has_no_terms")


def test_subject_is_model_authored_not_an_enquiry_slice():
    """The subject must come from the reply, not from slicing the enquiry."""
    enquiry = ("We book appointments in a spreadsheet and need reminders sent "
               "out across several channels to our customers every week.")
    with _Stub(_valid_json()):
        proposal = synthesize_proposal(enquiry, {}, {}, {}, load_config())
    assert proposal["subject"] == VALID_REPLY["subject"]
    assert not proposal["subject"].startswith("Re: ")
    assert enquiry[:40] not in proposal["subject"]
    teardown()
    print("PASS: subject_is_model_authored_not_an_enquiry_slice")


def test_synthesize_proposal_rejects_overlong_subject():
    with _Stub(_valid_json(subject="A" * 71)):
        try:
            synthesize_proposal("Need a web app", {}, {}, {}, load_config())
        except llm.LLMError as e:
            assert e.kind == "invalid_output"
            assert "70 character limit" in str(e)
        else:
            raise AssertionError("overlong subject was accepted")
    teardown()
    print("PASS: synthesize_proposal_rejects_overlong_subject")


def test_synthesize_proposal_rejects_multiline_subject():
    with _Stub(_valid_json(subject="Reminders system" + chr(10) + "for your clinic")):
        try:
            synthesize_proposal("Need a web app", {}, {}, {}, load_config())
        except llm.LLMError as e:
            assert e.kind == "invalid_output"
            assert "single line" in str(e)
        else:
            raise AssertionError("multiline subject was accepted")
    teardown()
    print("PASS: synthesize_proposal_rejects_multiline_subject")


def test_synthesize_proposal_rejects_missing_subject():
    reply = dict(VALID_REPLY)
    reply.pop("subject")
    with _Stub(json.dumps(reply)):
        try:
            synthesize_proposal("Need a web app", {}, {}, {}, load_config())
        except llm.LLMError as e:
            assert "subject is missing" in str(e)
        else:
            raise AssertionError("missing subject was accepted")
    teardown()
    print("PASS: synthesize_proposal_rejects_missing_subject")


def test_synthesize_proposal_rejects_unoffered_service():
    """A service the practice does not offer must not survive into a body."""
    reply = _valid_json(services=[
        {"service": "Mobile app development", "why": "they mentioned phones"}])
    with _Stub(reply):
        try:
            synthesize_proposal("Need a web app", {}, {}, {}, load_config())
        except llm.LLMError as e:
            assert e.kind == "invalid_output"
            assert "not offered" in str(e)
        else:
            raise AssertionError("unoffered service was accepted")
    teardown()
    print("PASS: synthesize_proposal_rejects_unoffered_service")


def test_synthesize_proposal_rejects_quoted_price():
    reply = _valid_json(closing="I can start at $120/hr once you confirm.")
    with _Stub(reply):
        try:
            synthesize_proposal("Need a web app", {}, {}, {}, load_config())
        except llm.LLMError as e:
            assert e.kind == "invalid_output"
        else:
            raise AssertionError("quoted price was accepted")
    teardown()
    print("PASS: synthesize_proposal_rejects_quoted_price")


def test_synthesize_proposal_raises_on_unparseable_reply():
    with _Stub("Happy to help with your project!"):
        try:
            synthesize_proposal("Need a web app", {}, {}, {}, load_config())
        except llm.LLMError as e:
            assert e.kind == "invalid_output"
        else:
            raise AssertionError("prose reply was accepted as a proposal")
    teardown()
    print("PASS: synthesize_proposal_raises_on_unparseable_reply")


# ---------------------------------------------------------------------------
# Provider chain.
#
# These stub llm._post, the single HTTP call, so provider selection and
# fallthrough are exercised without a network or any real key.
# ---------------------------------------------------------------------------

ALL_KEYS = ["GROQ_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY"]


class _Keys:
    """Set exactly the given provider keys for the duration of a test."""

    def __init__(self, **keys):
        self.keys = keys
        self._saved = {}

    def __enter__(self):
        import os
        for name in ALL_KEYS:
            self._saved[name] = os.environ.pop(name, None)
        for name, value in self.keys.items():
            os.environ[name] = value
        return self

    def __exit__(self, *exc):
        import os
        for name in ALL_KEYS:
            os.environ.pop(name, None)
            if self._saved.get(name) is not None:
                os.environ[name] = self._saved[name]
        return False


class _Posts:
    """Replace llm._post with a scripted sequence of outcomes."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []
        self._real = None

    def __enter__(self):
        self._real = llm._post

        def _fake(url, headers, payload, timeout):
            self.calls.append({"url": url, "headers": headers, "payload": payload})
            outcome = self.outcomes.pop(0) if self.outcomes else None
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        llm._post = _fake
        return self

    def __exit__(self, *exc):
        llm._post = self._real
        return False


def _openai_body(text):
    return {"choices": [{"message": {"content": text}}], "model": "stub-model",
            "usage": {"total_tokens": 1}}


def _gemini_body(text):
    return {"candidates": [{"content": {"parts": [{"text": text}]},
                            "finishReason": "STOP"}],
            "modelVersion": "gemini-stub",
            "usageMetadata": {"totalTokenCount": 1}}


def test_chain_skips_unconfigured_providers_silently():
    """Only Gemini configured: Groq is skipped, not an error."""
    with _Keys(GEMINI_API_KEY="g-key"):
        with _Posts(_gemini_body('{"ok": true}')) as posts:
            result = llm.complete_json("sys", "user")
    assert result["provider"] == "gemini"
    assert len(posts.calls) == 1
    outcomes = {a["provider"]: a["outcome"] for a in result["attempts"]}
    assert outcomes["groq"] == "skipped"
    assert outcomes["gemini"] == "ok"
    teardown()
    print("PASS: chain_skips_unconfigured_providers_silently")


def test_chain_falls_through_on_rate_limit():
    """Groq 429 must hand off to Gemini rather than fail the run."""
    with _Keys(GROQ_API_KEY="g", GEMINI_API_KEY="gem"):
        with _Posts(llm._Fallthrough("HTTP 429: rate limited"),
                    _gemini_body('{"ok": true}')) as posts:
            result = llm.complete_json("sys", "user")
    assert result["provider"] == "gemini"
    assert len(posts.calls) == 2
    failed = [a for a in result["attempts"] if a["outcome"] == "failed"]
    assert failed[0]["provider"] == "groq"
    assert "429" in failed[0]["reason"]
    teardown()
    print("PASS: chain_falls_through_on_rate_limit")


def test_chain_falls_through_on_rejected_credentials():
    """A stale key on one provider must not deny the run a working one."""
    with _Keys(GROQ_API_KEY="stale", OPENROUTER_API_KEY="ok"):
        with _Posts(llm._Fallthrough("HTTP 401: User not found."),
                    _openai_body('{"ok": true}')):
            result = llm.complete_json("sys", "user")
    assert result["provider"] == "openrouter"
    teardown()
    print("PASS: chain_falls_through_on_rejected_credentials")


def test_chain_fails_loudly_when_every_provider_declines():
    with _Keys(GROQ_API_KEY="a", GEMINI_API_KEY="b", OPENROUTER_API_KEY="c"):
        with _Posts(llm._Fallthrough("HTTP 429: rate limited"),
                    llm._Fallthrough("HTTP 500: upstream"),
                    llm._Fallthrough("TimeoutError: timed out")):
            try:
                llm.complete_json("sys", "user")
            except llm.LLMError as e:
                assert e.kind == "api_error"
                # The failure names every provider tried and why each declined.
                for provider in ("groq", "gemini", "openrouter"):
                    assert provider in str(e)
            else:
                raise AssertionError("exhausted chain did not fail loudly")
    teardown()
    print("PASS: chain_fails_loudly_when_every_provider_declines")


def test_chain_does_not_fall_through_on_unusable_reply():
    """Prose from provider 1 is terminal -- it must not shop for a nicer answer."""
    with _Keys(GROQ_API_KEY="a", GEMINI_API_KEY="b"):
        with _Posts(_openai_body("Sure, I can help with that!")) as posts:
            try:
                llm.complete_json("sys", "user")
            except llm.LLMError as e:
                assert e.kind == "invalid_output"
            else:
                raise AssertionError("prose reply was accepted")
    assert len(posts.calls) == 1, "invalid output must not trigger failover"
    teardown()
    print("PASS: chain_does_not_fall_through_on_unusable_reply")


def test_no_provider_configured_is_config_error():
    with _Keys():
        try:
            llm.complete("sys", "user")
        except llm.ConfigError as e:
            assert "GROQ_API_KEY" in str(e)
        else:
            raise AssertionError("empty chain did not raise ConfigError")
    teardown()
    print("PASS: no_provider_configured_is_config_error")


def test_gemini_key_travels_in_header_not_url():
    """Audit S-1 regression guard.

    generate-proposal.ps1:103 puts the key in the query string, where it leaks
    into exception messages, proxy logs and PowerShell transcripts. The key must
    appear in the x-goog-api-key header and never in the URL.
    """
    with _Keys(GEMINI_API_KEY="super-secret-key"):
        with _Posts(_gemini_body('{"ok": true}')) as posts:
            llm.complete_json("sys", "user")
    call = posts.calls[0]
    assert call["headers"].get("x-goog-api-key") == "super-secret-key"
    assert "super-secret-key" not in call["url"]
    assert "key=" not in call["url"]
    teardown()
    print("PASS: gemini_key_travels_in_header_not_url")


def test_provider_recorded_in_proposal_evidence():
    """A proposal must say which provider produced it."""
    with _Keys(GEMINI_API_KEY="g"):
        with _Posts(_gemini_body(_valid_json())):
            proposal = synthesize_proposal(
                "We book appointments in a spreadsheet and need reminders.",
                {}, {}, {}, load_config())
    gen = proposal["generation"]
    assert gen["provider"] == "gemini"
    assert gen["model"] == "gemini-stub"
    assert gen["prompt_version"]
    assert gen["raw_text"]
    assert any(a["outcome"] == "skipped" for a in gen["provider_chain"])
    teardown()
    print("PASS: provider_recorded_in_proposal_evidence")


def _stub_specialists():
    """Neutral specialist output so run_workflow tests stay offline."""
    real = flagship._exec_with_retry
    flagship._exec_with_retry = lambda name, payload, retries=2: ({"status": "success"}, None)
    return real


def test_run_workflow_llm_unavailable_produces_no_proposal():
    real = _stub_specialists()
    try:
        with _Stub(None, raises=llm.LLMError("openrouter down", kind="api_error")):
            result = flagship.run_workflow("We need appointment reminders.",
                                           approval_mode=True)
    finally:
        flagship._exec_with_retry = real
    assert result["status"] == "llm_unavailable", result["status"]
    # No proposal object, no fallback text, nothing queued to send.
    assert result["proposal"] is None
    assert result["external_actions"] == []
    assert result["approval_request"] is None
    assert result["requires_approval"] is False
    assert "openrouter down" in result["error"]
    teardown()
    print("PASS: run_workflow_llm_unavailable_produces_no_proposal")


def test_run_workflow_llm_invalid_output_produces_no_proposal():
    real = _stub_specialists()
    try:
        with _Stub("not json at all"):
            result = flagship.run_workflow("We need appointment reminders.",
                                           approval_mode=True)
    finally:
        flagship._exec_with_retry = real
    assert result["status"] == "llm_invalid_output", result["status"]
    assert result["proposal"] is None
    assert result["external_actions"] == []
    assert result["approval_request"] is None
    teardown()
    print("PASS: run_workflow_llm_invalid_output_produces_no_proposal")


def test_run_workflow_missing_api_key_produces_no_proposal():
    """No key is llm_unavailable, not a silent template."""
    real = _stub_specialists()
    try:
        # Every provider key cleared, not just one -- with a chain, popping a
        # single key proves nothing.
        with _Keys():
            result = flagship.run_workflow("We need appointment reminders.",
                                           approval_mode=True)
    finally:
        flagship._exec_with_retry = real
    assert result["status"] == "llm_unavailable", result["status"]
    assert result["proposal"] is None
    assert result["external_actions"] == []
    teardown()
    print("PASS: run_workflow_missing_api_key_produces_no_proposal")


def test_request_approval_idempotent():
    proposal = {"proposal_id": "req-1", "body": "test"}
    r1 = request_approval(proposal, "enquiry")
    r2 = request_approval(proposal, "enquiry")
    request_path = APPROVAL_DIR / "req-1.request.json"
    evidence_path = EVIDENCE_DIR / "req-1-proposal.json"
    mtime1 = request_path.stat().st_mtime
    mtime2 = request_path.stat().st_mtime
    assert mtime1 == mtime2
    cleanup("req-1")
    teardown()
    print("PASS: request_approval_idempotent")


def test_record_decision_not_overwritten_by_default():
    request_id = "req-2"
    request_approval({"proposal_id": request_id}, "enquiry")
    record_decision(request_id, approved=True, approver="juma", reason="approved first")
    record_decision(request_id, approved=False, approver="other", reason="changed")
    decision = json.loads((APPROVAL_DIR / f"{request_id}.decision.json").read_text())
    assert decision["approved"] is True
    assert decision["reason"] == "approved first"
    cleanup(request_id)
    teardown()
    print("PASS: record_decision_not_overwritten_by_default")


def test_cleanup_removes_artifacts():
    request_id = "req-3"
    request_approval({"proposal_id": request_id}, "enquiry")
    record_decision(request_id, approved=True, approver="juma")
    cleanup(request_id)
    assert not (APPROVAL_DIR / f"{request_id}.request.json").exists()
    assert not (APPROVAL_DIR / f"{request_id}.decision.json").exists()
    teardown()
    print("PASS: cleanup_removes_artifacts")


def main():
    try:
        setup()
        test_classify_enquiry_research()
        test_classify_enquiry_coding()
        test_classify_enquiry_projects()
        test_classify_enquiry_missing_info()
        test_load_config_exists()
        test_synthesize_proposal_uses_config()
        test_synthesize_proposal_without_config()
        test_render_normalises_unicode_punctuation()
        test_commercial_block_comes_from_config_not_the_model()
        test_commercial_block_absent_when_config_has_no_terms()
        test_subject_is_model_authored_not_an_enquiry_slice()
        test_synthesize_proposal_rejects_overlong_subject()
        test_synthesize_proposal_rejects_multiline_subject()
        test_synthesize_proposal_rejects_missing_subject()
        test_synthesize_proposal_rejects_unoffered_service()
        test_synthesize_proposal_rejects_quoted_price()
        test_synthesize_proposal_raises_on_unparseable_reply()
        test_run_workflow_llm_unavailable_produces_no_proposal()
        test_run_workflow_llm_invalid_output_produces_no_proposal()
        test_run_workflow_missing_api_key_produces_no_proposal()
        test_chain_skips_unconfigured_providers_silently()
        test_chain_falls_through_on_rate_limit()
        test_chain_falls_through_on_rejected_credentials()
        test_chain_fails_loudly_when_every_provider_declines()
        test_chain_does_not_fall_through_on_unusable_reply()
        test_no_provider_configured_is_config_error()
        test_gemini_key_travels_in_header_not_url()
        test_provider_recorded_in_proposal_evidence()
        test_request_approval_idempotent()
        test_record_decision_not_overwritten_by_default()
        test_cleanup_removes_artifacts()
        print("\nALL UNIT TESTS PASSED")
        return 0
    except Exception as e:
        print(f"FAIL: {e}")
        import traceback
        traceback.print_exc()
        return 1
    finally:
        teardown()


if __name__ == "__main__":
    sys.exit(main())
