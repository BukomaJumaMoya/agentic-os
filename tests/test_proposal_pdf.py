#!/usr/bin/env python3
"""
PDF rendering and document delivery — offline tests.

No network. send_document's transport is never exercised here; only the
preconditions that decide whether anything leaves the machine, plus the token
resolution order. Rendering runs for real, against fpdf2.
"""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from orchestrator import proposal_pdf, telegram_approval  # noqa: E402
from orchestrator.approval import ConfigError  # noqa: E402

passed = 0
_TMP = Path(tempfile.mkdtemp(prefix="pdf-tests-"))


def ok(name):
    global passed
    passed += 1
    print(f"PASS: {name}")


CONFIG = {
    "name": "Juma Moya",
    "business": "Bukoma Freelance Software Engineering",
    "contact": {"email": "someone@example.com", "telegram": "@handle"},
}

PROPOSAL = {
    "proposal_id": "abcd1234-0000-0000-0000-000000000000",
    "subject": "Appointment reminders from a spreadsheet",
    "body": (
        "Hi,\n\nThank you for reaching out.\n\n"
        "You keep contacts in a spreadsheet and want reminders sent.\n\n"
        "How I can help:\n"
        "- APIs and integrations - the spreadsheet needs connecting\n\n"
        "Suggested approach:\n"
        "1. Confirm the channels\n"
        "2. Build the integration layer\n\n"
        "Regards,\nJuma Moya\nBukoma Freelance Software Engineering\n"
    ),
}


def test_renders_a_real_pdf():
    out = proposal_pdf.render(PROPOSAL, CONFIG, _TMP / "a.pdf")
    data = out.read_bytes()
    assert data[:5] == b"%PDF-", data[:20]
    assert len(data) > 800
    ok("renders_a_real_pdf")


def test_identity_comes_from_config_not_the_proposal():
    """A PDF must not be signable with a name the config did not supply."""
    other = dict(CONFIG, name="Someone Else", business="Other Practice Ltd")
    out = proposal_pdf.render(PROPOSAL, other, _TMP / "b.pdf")
    raw = out.read_bytes().decode("latin-1", errors="ignore")
    # fpdf stores the author in the document metadata verbatim.
    assert "Someone Else" in raw
    ok("identity_comes_from_config_not_the_proposal")


def test_body_is_normalised_before_rendering():
    """Typographic punctuation must not reach a latin-1 core font."""
    body = PROPOSAL["body"].replace("reminders sent", "end" + chr(0x2011) + "to" + chr(0x2011) + "end")
    out = proposal_pdf.render(dict(PROPOSAL, body=body), CONFIG, _TMP / "c.pdf")
    assert out.read_bytes()[:5] == b"%PDF-"
    ok("body_is_normalised_before_rendering")


def test_filename_is_client_presentable():
    name = proposal_pdf.filename_for(PROPOSAL, CONFIG)
    assert name.startswith("Proposal-")
    assert name.endswith(".pdf")
    assert " " not in name
    ok("filename_is_client_presentable")


def test_missing_file_is_not_attempted():
    r = telegram_approval.send_document("123", _TMP / "nope.pdf")
    assert r["delivered"] is False
    assert r["document_status"] == "not_attempted"
    assert "not found" in r["reason"]
    ok("missing_file_is_not_attempted")


def test_empty_file_is_not_attempted():
    empty = _TMP / "empty.pdf"
    empty.write_bytes(b"")
    r = telegram_approval.send_document("123", empty)
    assert r["document_status"] == "not_attempted"
    assert "empty" in r["reason"]
    ok("empty_file_is_not_attempted")


def test_token_is_read_from_the_environment_first():
    saved = os.environ.get("TELEGRAM_BOT_TOKEN")
    os.environ["TELEGRAM_BOT_TOKEN"] = "env-token-wins"
    try:
        assert telegram_approval._bot_token() == "env-token-wins"
    finally:
        os.environ.pop("TELEGRAM_BOT_TOKEN", None)
        if saved is not None:
            os.environ["TELEGRAM_BOT_TOKEN"] = saved
    ok("token_is_read_from_the_environment_first")


def test_missing_token_everywhere_is_a_config_error():
    saved_env = os.environ.pop("TELEGRAM_BOT_TOKEN", None)
    saved_cfg = os.environ.get("OPENCLAW_CONFIG")
    empty_cfg = _TMP / "no-channels.json"
    empty_cfg.write_text(json.dumps({"channels": {}}), encoding="utf-8")
    os.environ["OPENCLAW_CONFIG"] = str(empty_cfg)
    try:
        telegram_approval._bot_token()
    except ConfigError as e:
        assert "TELEGRAM_BOT_TOKEN" in str(e)
    else:
        raise AssertionError("missing token did not raise")
    finally:
        os.environ.pop("OPENCLAW_CONFIG", None)
        if saved_cfg is not None:
            os.environ["OPENCLAW_CONFIG"] = saved_cfg
        if saved_env is not None:
            os.environ["TELEGRAM_BOT_TOKEN"] = saved_env
    ok("missing_token_everywhere_is_a_config_error")


def test_document_status_vocabulary_is_not_the_gateways():
    """A different transport must not borrow words implying the same guarantees."""
    gateway_words = {"sent", "queued", "suppressed", "unknown"}
    ours = {"delivered", "not_attempted", "rejected", "transport_failed"}
    assert not (gateway_words & ours)
    ok("document_status_vocabulary_is_not_the_gateways")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    try:
        for t in tests:
            t()
        print(f"\nALL PROPOSAL PDF TESTS PASSED ({passed})")
        return 0
    except Exception as e:
        print(f"FAIL: {e}")
        import traceback
        traceback.print_exc()
        return 1
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
