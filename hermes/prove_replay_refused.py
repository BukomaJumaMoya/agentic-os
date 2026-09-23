#!/usr/bin/env python3
"""Is a replayed `/approve` refused? Answer it against Hermes' own queue.

THE QUESTION
------------
If the operator approves a write, and later sends the same `/approve` again --
or an attacker who has seen one replays it -- does the second one authorise a
second write?

THE ANSWER IS STRUCTURAL, NOT A CHECK SOMEBODY ADDED
----------------------------------------------------
There is no durable approval token to replay. Hermes' gate is per-CALL: the
tool call blocks inside `_await_gateway_decision`, which appends an entry to
`_gateway_queues[session]` and waits on its event.
`resolve_gateway_approval()` pops the entry out of that queue in the same
critical section in which it commits the choice. So:

    the approval and the call it authorises are the same object,
    and answering consumes it.

A replay therefore arrives to an empty queue and `resolve_gateway_approval`
returns 0 -- resolved nothing. The gateway tells the client nothing was
pending rather than acking "ok"; no tool runs.

The per-call elicitation patch (`hermes/patch_elicitation_percall.py`) is what
keeps it that way for MCP writes. Without it the prompt offers "Always Allow",
which stores a decision that OUTLIVES the call -- and that, not a replayed
message, is the real replay risk.

Run under HERMES' interpreter, not the agents' venv:

    %LOCALAPPDATA%\hermes\hermes-agent\venv\Scripts\python hermes/prove_replay_refused.py
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

HOME = Path(os.environ.get("LOCALAPPDATA", "")) / "hermes"
sys.path.insert(0, str(HOME / "hermes-agent"))

from tools import approval as A          # noqa: E402  - after the path insert

SESSION = "replay-proof"


class _Entry:
    """The shape `_await_gateway_decision` puts in the queue, minus the wait."""

    def __init__(self, request_id: str) -> None:
        self.data = {"request_id": request_id, "command": "pm_action",
                     "pattern_keys": []}
        self.result = None
        self.reason = None
        self.event = threading.Event()
        self.settle = None


def main() -> int:
    entry = _Entry("req-1")
    with A._lock:
        A._gateway_queues[SESSION] = [entry]

    first = A.resolve_gateway_approval(SESSION, "once", request_id="req-1")
    replay = A.resolve_gateway_approval(SESSION, "once", request_id="req-1")
    bare = A.resolve_gateway_approval(SESSION, "once")
    leftover = A._gateway_queues.get(SESSION)

    print(f"  first  /approve req-1 -> resolved {first}   (expect 1)")
    print(f"  replay /approve req-1 -> resolved {replay}   (expect 0)")
    print(f"  replay bare /approve  -> resolved {bare}   (expect 0)")
    print(f"  queue afterwards      -> {leftover!r}   (expect None)")
    print(f"  entry.result          -> {entry.result!r}   (consumed exactly once)")

    ok = (first, replay, bare, leftover) == (1, 0, 0, None)
    print("\n" + ("PASS: an approval is consumed by the call it authorised; "
                  "a replay resolves nothing."
                  if ok else "FAIL: a replayed approval resolved something."))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
