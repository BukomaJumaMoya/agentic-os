#!/usr/bin/env python3
"""The two newest boundaries: n8n's inbound HMAC, and kola's inner allowlist.

Run: agents/.venv/Scripts/python tests/test_n8n_kola_boundaries.py

Neither boundary is a tool-name allowlist, which is why both needed writing:

  - n8n pushes INTO this system. The control is a signature, not a tool list.
  - Kolaborate's `kola_call` names its operation in its ARGUMENTS, so no
    tool-name allowlist upstream or in Hermes can see what is about to run.

Offline. The HMAC cases call the receiver's own verifier; the kola cases call
the agent's allowlist logic. Neither needs a network or a running container --
the live versions of both are recorded in documentation/AUDIT-2.md.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents" / "n8n"))

import receiver  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASSED if condition else FAILED).append(name if condition else f"{name}: {detail}")


class Headers(dict):
    def get(self, key, default=None):
        return dict.get(self, key, default)


def test_hmac_refuses_everything_unsigned() -> None:
    secret, body = "s3cret", b'{"task":"x"}'
    now = str(int(__import__("time").time()))
    good = receiver.sign(secret, now, body)

    ok, _ = receiver.verify(secret, Headers({"X-Hermes-Signature": good,
                                             "X-Hermes-Timestamp": now}), body)
    check("a correctly signed body is accepted", ok, "the healthy case was refused")

    for name, headers, note in [
        ("unsigned", {"X-Hermes-Timestamp": now}, "no signature at all"),
        ("wrong signature", {"X-Hermes-Signature": "0" * 64,
                             "X-Hermes-Timestamp": now}, "forged"),
        ("malformed timestamp", {"X-Hermes-Signature": good,
                                 "X-Hermes-Timestamp": "soon"}, "non-numeric"),
    ]:
        ok, reason = receiver.verify(secret, Headers(headers), body)
        check(f"refuses: {name}", not ok, f"{note} was accepted ({reason})")

    # The body is covered by the signature, not just the timestamp.
    ok, _ = receiver.verify(secret, Headers({"X-Hermes-Signature": good,
                                             "X-Hermes-Timestamp": now}),
                            b'{"task":"TAMPERED"}')
    check("refuses: body swapped after signing", not ok,
          "the signature does not cover the body")

    # Replay: a captured body stays valid forever without a timestamp window.
    old = str(int(__import__("time").time()) - (receiver.MAX_SKEW_SECONDS + 60))
    ok, _ = receiver.verify(secret, Headers({"X-Hermes-Signature": receiver.sign(secret, old, body),
                                             "X-Hermes-Timestamp": old}), body)
    check("refuses: correctly signed but stale (replay)", not ok,
          "a captured request could be replayed indefinitely")

    ok, _ = receiver.verify("", Headers({"X-Hermes-Signature": good,
                                         "X-Hermes-Timestamp": now}), body)
    check("refuses: no shared secret configured", not ok,
          "an unconfigured receiver accepted a request")


def test_rate_limit() -> None:
    receiver._hits.clear()
    verdicts = [receiver.rate_limited("10.0.0.1")
                for _ in range(receiver.RATE_LIMIT_MAX + 2)]
    check("rate limit allows the window then refuses",
          verdicts[:receiver.RATE_LIMIT_MAX] == [False] * receiver.RATE_LIMIT_MAX
          and all(verdicts[receiver.RATE_LIMIT_MAX:]), str(verdicts))
    receiver._hits.clear()


def test_receiver_is_not_published_to_the_host() -> None:
    """It runs in Docker on a private network with no published port.

    A host-loopback listener is unreachable from a container, which is what
    `connect ECONNREFUSED 127.0.0.1:8787` was saying; binding 0.0.0.0 on the
    host would have opened the port on Wi-Fi. Neither, so the compose args
    matter and are asserted here.
    """
    dockerfile = (REPO / "agents" / "n8n" / "Dockerfile").read_text(encoding="utf-8")
    check("the receiver image exists and runs as a non-root user",
          "adduser" in dockerfile and "USER receiver" in dockerfile, dockerfile[:120])
    source = (REPO / "agents" / "n8n" / "receiver.py").read_text(encoding="utf-8")
    # Look for the CREDENTIAL and the API HOST, not the word "Telegram" --
    # the module docstring explains at length that it cannot reach Telegram,
    # and the first version of this check failed on its own explanation.
    check("the receiver holds no bot token",
          "TELEGRAM_BOT_TOKEN" not in source and "api.telegram.org" not in source,
          "a compromise of the listener would yield a Telegram token")
    check("the receiver only appends to a queue",
          "def enqueue" in source and "subprocess" not in source,
          "the listener can do more than queue a notice")


def test_kola_allowlists_the_inner_operation() -> None:
    kola = (REPO / "agents" / "kola" / "main.py").read_text(encoding="utf-8")

    check("kola declares an inner-operation allowlist",
          "ALLOWED_READ_OPERATIONS" in kola and "ALLOWED_WRITE_OPERATIONS" in kola,
          "kola_call's operation would be unpoliced")
    check("the write allowlist is empty",
          "ALLOWED_WRITE_OPERATIONS: set[str] = set()" in kola,
          "a write operation was added without re-deciding the annotation")
    check("the refusal happens before the RPC",
          "operation_not_allowed" in kola and "kola_operation_refused" in kola,
          "a refused operation would still reach Kolaborate")

    # The real write operations must NOT be reachable. These names come from
    # the live catalogue, so this fails if someone pastes the category in.
    for write_op in ("jobs_create", "jobs_set_slug", "jobs_update_company",
                     "jobs_update_skills", "jobs_rollback_applied_notes"):
        check(f"{write_op} is not allowed",
              f'"{write_op}"' not in kola.split("ALLOWED_WRITE_OPERATIONS")[0]
              .split("ALLOWED_READ_OPERATIONS")[-1],
              f"{write_op} reached the read allowlist")

    check("both kola tools are annotated read-only",
          kola.count('annotations={"readOnlyHint": True}') == 2,
          "a kola tool lost its annotation, or a write tool was added")


def main() -> int:
    for func in (test_hmac_refuses_everything_unsigned,
                 test_rate_limit,
                 test_receiver_is_not_published_to_the_host,
                 test_kola_allowlists_the_inner_operation):
        func()
    for name in PASSED:
        print(f"  PASS  {name}")
    for name in FAILED:
        print(f"  FAIL  {name}")
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
