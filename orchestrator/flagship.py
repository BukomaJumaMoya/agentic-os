#!/usr/bin/env python3
"""
Flagship End-to-End Workflow — client enquiry → proposal → approval → action.

This module implements the bounded flagship workflow for the freelance
engineering agentic stack. It is a deterministic pipeline, not an
open-ended autonomous loop.
"""

import json
import os
import sys
import uuid
from pathlib import Path
from datetime import datetime, timezone

BASE = Path(__file__).resolve().parent.parent

# Repo root ahead of this file's own directory, before the first orchestrator
# import -- see the note in telegram_commands.py. orchestrator/orchestrator.py
# shadows the package when this module runs as a script.
sys.path.insert(0, str(BASE))

from orchestrator.approval import request_approval, ConfigError  # noqa: E402
from orchestrator.llm import LLMError  # noqa: E402
from orchestrator.proposal import synthesize_proposal  # noqa: E402
from orchestrator.research_query import extract_queries  # noqa: E402

# Mirrors orchestrator.approval.STATE_ROOT -- one knob to relocate runtime
# state out of the repo tree (audit S-7 / P0-5).
STATE_ROOT = Path(os.getenv("AGENTIC_STATE_DIR") or BASE).resolve()
APPROVAL_DIR = STATE_ROOT / ".approval"
EVIDENCE_DIR = STATE_ROOT / "evidence"
CONFIG_PATH = BASE / "config" / "juma.json"


def load_config():
    if not CONFIG_PATH.exists():
        return {}
    try:
        # See the note in approval.load_config -- the file is UTF-8 and
        # read_text() would otherwise decode it as cp1252.
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def classify_enquiry(text: str) -> dict:
    t = text.lower()
    signals = {
        "research": ["research", "compare", "competitor", "market", "technology", "find", "evaluate", "analyze", "review", "architecture", "design", "assess", "survey"],
        "projects": ["clickup", "task", "project", "update", "status", "list", "create task", "onboarding", "milestone", "backlog", "sprint"],
        "coding": ["code", "bug", "debug", "generate", "build", "api", "integration", "develop", "refactor", "test", "review code", "python", "javascript", "typescript"],
        "proposal": ["proposal", "quote", "estimate", "price", "budget", "engagement", "hire", "contract", "statement of work"],
    }
    hits = {}
    for domain, kws in signals.items():
        hits[domain] = [kw for kw in kws if kw in t]

    # All three, by construction, for an inbound client enquiry.
    #
    # Keyword selection did not work and could not: the Brown Optical enquiry
    # scored zero hits across every domain list, so it fell through to the
    # ["research"] default and projects and coding never ran at all. The default
    # hid the miss rather than surfacing it.
    #
    # Each agent covers a dimension that is relevant to any enquiry by
    # definition -- prior experience, technical feasibility, domain research --
    # and each already reports honestly when it finds nothing. Deciding
    # relevance up front, from keywords, discarded evidence before anyone looked
    # at it. The hits are still recorded, as a reason rather than a gate.
    selected_agents = ["research", "projects", "coding"]

    missing = []
    if "budget" not in t and "quote" not in t and "estimate" not in t:
        missing.append("budget")
    if "timeline" not in t and "deadline" not in t and "when" not in t:
        missing.append("timeline")
    if "scope" not in t and "requirements" not in t and "details" not in t:
        missing.append("scope")

    return {
        "domain_hits": {k: v for k, v in hits.items() if v},
        "selected_agents": selected_agents,
        "missing_information": missing,
    }


# synthesize_proposal lives in orchestrator/proposal.py and is re-exported here
# so that `from orchestrator.flagship import synthesize_proposal` keeps working.
# The template that used to sit at this spot is gone on purpose: it emitted the
# same three bullet points for every enquiry, which read as a considered reply
# while containing nothing about the client. There is no fallback to it.


def _exec_with_retry(name, payload, retries=2):
    from orchestrator.orchestrator import invoke
    output, error = invoke(name, payload, retries=retries)
    return output, error


def _synthesis_failed(status, error, enquiry, classification, specialist_outputs,
                      specialist_errors, retry_log, detail=None, config=None):
    """Terminal result for an enquiry whose proposal could not be drafted.

    Deliberately carries no ``proposal`` key rather than an empty one: a caller
    that reaches for result["proposal"] should raise, not quietly send "". No
    approval request is filed and no external action is queued, so there is
    nothing for a human to approve and nothing for a retry to pick up.
    """
    return {
        "workflow": "flagship",
        "version": "1.0.0",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "enquiry": enquiry,
        "classification": classification,
        "agents_invoked": classification.get("selected_agents", []),
        "specialist_outputs": specialist_outputs,
        "proposal": None,
        "verification": {
            "config_present": bool(config),
            "proposal_non_empty": False,
            "missing_information_identified": bool(classification.get("missing_information")),
            "agents_selected": classification.get("selected_agents"),
            "specialist_errors": specialist_errors,
            "retry_log": retry_log,
        },
        "requires_approval": False,
        "approval_prompt_delivered": None,
        "approval_request": None,
        "external_actions": [],
        "status": status,
        "error": error,
        "error_detail": detail,
        "retry_log": retry_log,
        "specialist_errors": specialist_errors,
        "recovery": {
            "resume_with": "No proposal was produced. Fix the cause below and re-run "
                           "the enquiry; there is no approval request to resume.",
            "do_not_retry_external_action_automatically": True,
            "telegram_commands": [],
        },
    }


def _deliver_proposal_document(proposal: dict, config: dict) -> dict:
    """Render the proposal as a PDF and send it as a Telegram document.

    Entirely best effort. Every failure here is reported and none of them
    changes the approval outcome: the plain-text proposal is the canonical
    artefact and it has already been delivered in the prompt. A missing PDF is
    a missing convenience, not a missing proposal.
    """
    try:
        from orchestrator import proposal_pdf
        from orchestrator.telegram_approval import send_document, _allowed_user_id
    except Exception as e:
        return {"attempted": False, "document_status": "not_attempted",
                "reason": f"delivery modules unavailable: {e}"}

    if not proposal_pdf.available():
        return {"attempted": False, "document_status": "not_attempted",
                "reason": "fpdf2 is not installed; proposal delivered as text only"}

    try:
        out_dir = STATE_ROOT / "evidence"
        path = proposal_pdf.render(
            proposal, config,
            out_dir / proposal_pdf.filename_for(proposal, config))
    except Exception as e:
        return {"attempted": True, "document_status": "render_failed",
                "reason": f"{type(e).__name__}: {e}"}

    try:
        result = send_document(
            _allowed_user_id(), path,
            caption=f"Proposal draft - {proposal.get('subject') or ''}"[:1024])
    except Exception as e:
        return {"attempted": True, "document_status": "transport_failed",
                "reason": f"{type(e).__name__}: {e}", "path": str(path)}
    result["attempted"] = True
    result["path"] = str(path)
    return result


def run_workflow(enquiry: str, approval_mode: bool = True, force_agent: str = None) -> dict:
    """Legacy entry point for the enquiry -> proposal workflow.

    Kept permanently rather than migrating roughly twenty call sites. It costs
    one function, and it keeps the proof of the registry move re-runnable: every
    existing suite exercises this signature unchanged.

    New callers should use workflow.run(name, inputs).
    """
    from orchestrator import workflow as _workflow
    return _workflow.run("proposal", {"enquiry": enquiry},
                         approval_mode=approval_mode, force_agent=force_agent)


def main():
    try:
        data = json.load(sys.stdin)
    except Exception as e:
        print(json.dumps({"error": f"Invalid input JSON: {e}"}))
        sys.exit(1)

    enquiry = data.get("enquiry") or data.get("task") or ""
    approval_mode = data.get("approval_mode", True)
    result = run_workflow(enquiry, approval_mode=approval_mode)
    print(json.dumps(result, indent=2))
    sys.exit(0)


if __name__ == "__main__":
    main()