"""Make outbound delivery structurally impossible for this process.

Import this before any orchestrator module. ``_state_isolation`` imports it, and
``conftest.py`` imports it again for pytest collection, so both ways of running a
suite are covered.

Why this exists
---------------
Six identical proposal PDFs arrived on the operator's phone from a test sweep.
Nobody ran the workflow six times: the e2e, failure-injection and integration
suites each call ``run_workflow``, ``_llm_stub`` makes the proposal succeed, and
``run_workflow`` then delivers the PDF over the Telegram Bot API. Those suites
mock ``send_approval_prompt``; nothing mocked document delivery, because the
document path was added afterwards and no one thought to.

That is the failure mode this guard exists to remove. "Remember to mock the
sending function" is not a safety property -- it holds until someone adds a
second sending function, which is exactly what happened. The guard lives inside
the outbound functions instead, so a suite that forgets to mock is refused by
the transport rather than trusted not to reach it.

Reading the environment, not a Python flag, because the agents are subprocesses:
a child process inherits the variable and is blocked too.
"""

import os

ENV_FLAG = "AGENTIC_BLOCK_OUTBOUND"

os.environ[ENV_FLAG] = "1"
