"""Install a deterministic offline stub for ``orchestrator.llm.complete``.

MUST be imported AFTER ``_state_isolation`` and BEFORE any test calls
``run_workflow``.

Why this exists
---------------
``flagship.run_workflow`` now drafts proposals through a model. The e2e,
failure-injection and integration suites drive the full pipeline, so without a
stub they need a live provider key -- which CI does not have and should not
have. They would fail with ``llm_unavailable`` on every assertion about a
proposal, for a reason that has nothing to do with what they test.

The seam is ``llm.complete``: one function, the single point where the process
would otherwise open a socket to a provider. Everything above it still runs for
real in these suites -- prompt assembly, ``complete_json`` parsing,
``proposal._validate``, ``proposal._render``, approval-record creation and
evidence writing. Patching ``synthesize_proposal`` instead would hollow out the
very path these suites exist to cover.

The specialist agents are deliberately NOT stubbed. They are subprocesses with
their own credentials, and letting them fail the way they fail in CI -- research
without TAVILY_API_KEY, projects without CLICKUP_TOKEN -- is part of what these
suites verify: that the workflow degrades honestly rather than inventing
evidence.
"""

import json

from orchestrator import llm

STUB_MODEL = "stub/offline-model"

# Deliberately generic so it validates against any enquiry these suites use:
# "Web applications" is present in config/juma.json's services list, which is
# what proposal._validate checks against.
REPLY = {
    "subject": "Initial response to your project enquiry",
    "understanding": (
        "You have described a piece of software you need built and the problem "
        "it should solve. The scope, budget and timing are not yet settled."
    ),
    "services": [
        {"service": "Web applications",
         "why": "you described a system your team would use directly"},
    ],
    "approach": [
        "Confirm the requirements and how the work is done today.",
        "Agree the smallest useful first delivery.",
        "Build it, then review against the agreed scope.",
    ],
    "clarifications": [
        "What does the current process look like end to end?",
        "Who will be using this day to day?",
    ],
    "closing": "Answer those and I can scope the work properly.",
}

# Every stubbed call, so a test can assert the model was consulted at all.
CALLS = []


def _fake_complete(system, user, **kwargs):
    CALLS.append({"system": system, "user": user, "kwargs": kwargs})
    return {
        "text": json.dumps(REPLY),
        "model": STUB_MODEL,
        "provider": "stub",
        "attempts": [{"provider": "stub", "model": STUB_MODEL, "outcome": "ok"}],
        "usage": {"total_tokens": 0},
        "raw": {"stub": True},
    }


llm.complete = _fake_complete
