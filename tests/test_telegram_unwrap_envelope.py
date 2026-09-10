#!/usr/bin/env python3
"""
Regression: conversations_send's real response envelope.

Observed live on 2026-09-10. POST /tools/invoke returned HTTP 200 with
ok:true and the envelope below, but _unwrap_tool_result did not know the
payload lives under "details". It returned the outer dict unchanged, so
status came back None and a message that HAD been delivered was reported as
delivery_status="unrecognised_status" -- a false negative on a real send.

This locks the observed shape rather than a guessed one. Note the payload is
NOT recoverable by the unwrapper from content[0].text: that is a JSON string,
not a dict, so the isinstance(inner, (dict, list)) check skips it. "details"
is the only field the unwrapper can read.

The conversationRef and queueId hex are same-shape dummies, not the live
values: the real ones address the operator's private Telegram DM and this
repository is public (same reasoning as stripping the allowlist chat id,
audit S-4). Key names, nesting, types and lengths are unchanged.

No network calls. Does not read or write the live conversation cache.
"""

import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from orchestrator.telegram_approval import _unwrap_tool_result  # noqa: E402

_REF = "conv_" + "0" * 32
_QUEUE = "convq_" + "1" * 32

# Exact envelope shape returned by conversations_send via /tools/invoke.
CONVERSATIONS_SEND_ENVELOPE = {
    "content": [
        {
            "type": "text",
            "text": (
                '{\n  "status": "sent",\n'
                f'  "conversationRef": "{_REF}",\n'
                '  "channel": "telegram",\n'
                '  "messageId": "140",\n'
                f'  "queueId": "{_QUEUE}"\n}}'
            ),
        }
    ],
    "details": {
        "status": "sent",
        "conversationRef": _REF,
        "channel": "telegram",
        "messageId": "140",
        "queueId": _QUEUE,
    },
}


def test_unwraps_details_to_payload_with_status_sent():
    payload = _unwrap_tool_result(CONVERSATIONS_SEND_ENVELOPE)
    assert isinstance(payload, dict), f"expected dict, got {type(payload).__name__}"
    assert payload.get("status") == "sent", f"status was {payload.get('status')!r}"
    assert payload is CONVERSATIONS_SEND_ENVELOPE["details"], "did not return the details dict"
    for key in ("conversationRef", "channel", "messageId", "queueId"):
        assert key in payload, f"missing {key}"
    assert re.fullmatch(r"conv_[a-f0-9]{32}", payload["conversationRef"])
    print("PASS: unwraps_details_to_payload_with_status_sent")


def test_outer_envelope_alone_has_no_status():
    """Documents why this mattered: the bug was reading status off the outer dict."""
    assert CONVERSATIONS_SEND_ENVELOPE.get("status") is None
    print("PASS: outer_envelope_alone_has_no_status")


def test_already_unwrapped_payload_passes_through():
    payload = {"status": "queued", "conversationRef": _REF, "channel": "telegram"}
    assert _unwrap_tool_result(payload) is payload
    print("PASS: already_unwrapped_payload_passes_through")


def test_conversations_list_shape_still_passes_through():
    """The other observed payload shape must keep working."""
    listed = {"conversations": [{"conversationRef": _REF, "kind": "direct"}]}
    assert _unwrap_tool_result(listed) is listed
    print("PASS: conversations_list_shape_still_passes_through")


def test_non_dict_input_is_returned_unchanged():
    for value in (None, "text", 42, ["a"]):
        assert _unwrap_tool_result(value) == value
    print("PASS: non_dict_input_is_returned_unchanged")


def main():
    try:
        test_unwraps_details_to_payload_with_status_sent()
        test_outer_envelope_alone_has_no_status()
        test_already_unwrapped_payload_passes_through()
        test_conversations_list_shape_still_passes_through()
        test_non_dict_input_is_returned_unchanged()
        print("\nALL ENVELOPE TESTS PASSED")
        return 0
    except AssertionError as e:
        print(f"FAIL: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
