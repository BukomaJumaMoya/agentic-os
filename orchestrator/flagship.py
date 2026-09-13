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
    if not enquiry or not enquiry.strip():
        return {
            "workflow": "flagship",
            "status": "error",
            "error": "Missing enquiry text",
        }

    # Enforce approval boundary in production paths
    if not approval_mode:
        return {
            "workflow": "flagship",
            "status": "error",
            "error": "approval_mode=False is not permitted in production; external actions require explicit approval",
        }

    # Disallow arbitrary agent override; classification must drive selection
    if force_agent:
        return {
            "workflow": "flagship",
            "status": "error",
            "error": "force_agent is not permitted in production; agent selection must follow classification",
        }

    classification = classify_enquiry(enquiry)
    config = load_config()

    research_findings = {}
    research_query_meta = {}
    project_context = {}
    coding_context = {}
    specialist_errors = []
    external_actions = []
    retry_log = []

    # Derived once and shared: research searches the web with it, projects
    # searches prior work with it. Both need the problem domain rather than the
    # client's prose, and neither should pay for a second extraction.
    query_extraction = None
    query_error = None
    if {"research", "projects"} & set(classification["selected_agents"]):
        try:
            query_extraction = extract_queries(enquiry)
        except (ConfigError, LLMError) as e:
            query_error = str(e)

    for agent in classification["selected_agents"]:
        payload = {}
        if agent == "research":
            # Never search the client's prose. enquiry[:200] sent their words --
            # company name included -- to a third-party search API and returned
            # CRM templates and a YouTube video for an appointment-reminder
            # problem. A model names the domain instead.
            if query_error is not None:
                research_findings = {
                    "agent": "research", "queries": [], "findings": [],
                    "facts": [], "assumptions": [],
                    "unknowns": ["No research was attempted: search queries "
                                 "could not be derived from the enquiry."],
                    "status": "skipped_no_queries",
                    "search": {"outcome": "skipped", "detail": query_error[:300]},
                }
                specialist_errors.append(
                    {"agent": agent, "error": f"research skipped: {query_error}"})
                continue
            extraction = query_extraction or {"queries": []}
            if not extraction["queries"]:
                reason = extraction.get("skipped_reason") or \
                    "the enquiry was too vague to search for usefully"
                research_findings = {
                    "agent": "research", "queries": [], "findings": [],
                    "facts": [], "assumptions": [],
                    "unknowns": [f"No research was attempted: {reason}"],
                    "status": "skipped_no_queries",
                    "search": {"outcome": "skipped", "detail": reason},
                    "query_extraction": extraction.get("generation"),
                }
                continue
            payload = {"queries": extraction["queries"], "max_sources": 3,
                       "fetch_content": True}
            research_query_meta = extraction
        elif agent == "projects":
            # Read-only, and searched by domain rather than by the client's
            # prose -- enquiry[:100] had the same defect the research query did.
            # No task is created at proposal time.
            domain = (query_extraction or {}).get("domain") or ""
            if not domain:
                project_context = {
                    "agent": "projects", "status": "skipped_no_query",
                    "detail": "no problem domain could be derived from the enquiry",
                }
                continue
            payload = {"action": "search_tasks", "query": domain}
        elif agent == "coding":
            # The enquiry is prose, not source. The previous payload passed it
            # as `code` to an `explain` action, asking the agent to read a
            # client's sentences as if they were Python.
            payload = {"action": "feasibility", "prompt": enquiry,
                       "language": "python"}

        # bounded retry for transient/unexpected tool responses
        output, error = _exec_with_retry(agent, payload, retries=2)
        attempt_info = {"agent": agent, "attempts": 2 if error else 1, "error": error}
        if error:
            retry_log.append(attempt_info)
            specialist_errors.append({"agent": agent, "error": error})
            if agent == "research":
                research_findings = {"error": error, "findings": [], "facts": [], "assumptions": [], "unknowns": ["Research tool unavailable"]}
            elif agent == "projects":
                project_context = {"error": error, "status": "error"}
            elif agent == "coding":
                coding_context = {"error": error, "status": "error"}
            continue

        if agent == "research":
            output["query_extraction"] = {
                "queries": research_query_meta.get("queries"),
                "domain": research_query_meta.get("domain"),
                "generation": research_query_meta.get("generation"),
            }
            research_findings = output
            # blocked / search_failed / no_results are all "no evidence", but
            # they are recorded distinctly so a provider block is never filed
            # as an empty search.
            research_status = research_findings.get("status")
            if research_status in ("no_results", "blocked", "search_failed"):
                detail = (research_findings.get("search") or {}).get("detail")
                msg = f"Research returned {research_status}"
                if detail:
                    msg = f"{msg}: {detail}"
                if msg not in specialist_errors:
                    specialist_errors.append({"agent": agent, "error": msg})
                if msg not in retry_log:
                    retry_log.append({"agent": agent, "attempts": 1, "error": msg})
                research_findings = {
                    "agent": "research",
                    "query": payload.get("query", ""),
                    "findings": [],
                    "facts": [],
                    "assumptions": ["No live research sources were reachable."],
                    "unknowns": ["Unable to gather external evidence for the requested research."],
                    "status": "fallback_no_results",
                    # Keep why, not just that. Without these the fallback erases
                    # the blocked/failed/empty distinction the agent just made.
                    "research_status": research_status,
                    "search": research_findings.get("search"),
                }
        elif agent == "projects":
            # Counts only. Task names in this workspace identify OTHER clients
            # -- "Beta Industries - Mobile App Development Proposal" is a real
            # example -- and a name reaching the proposal prompt is one client's
            # identity appearing in another client's document. Passing only a
            # count makes that leak impossible rather than filtered.
            # An agent whose output does not match the expected shape must not
            # take down the workflow: record nothing for that dimension and
            # carry on, exactly as if it had returned nothing.
            block = (output or {}).get("result")
            block = block if isinstance(block, dict) else {}
            tasks = block.get("tasks") or []
            project_context = {
                "agent": "projects",
                "status": output.get("status", "success"),
                "prior_engagements": len(tasks),
                "matched_domain": (query_extraction or {}).get("domain") or "",
                "scanned": block.get("scanned"),
                "note": "counts only; task names are withheld because they "
                        "identify other clients",
            }
        elif agent == "coding":
            result_block = (output or {}).get("result")
            result_block = result_block if isinstance(result_block, dict) else {}
            coding_context = {
                "agent": "coding",
                "status": output.get("status", "success"),
                "summary": result_block.get("summary", ""),
                "components": result_block.get("components") or [],
                "risks": result_block.get("findings") or [],
                "unknowns": result_block.get("unknowns") or [],
                "generation": (output or {}).get("generation"),
            }

    specialist_outputs = {
        "research": research_findings,
        "projects": project_context,
        "coding": coding_context,
    }

    # Synthesis is the point of no return for content: past here every field the
    # client would read exists. If the model could not produce one, the workflow
    # terminates here with no proposal object at all -- no template, no partial
    # draft, nothing an approver could mistake for a reply awaiting a decision.
    try:
        proposal = synthesize_proposal(
            enquiry, research_findings, project_context, coding_context, config)
    except ConfigError as e:
        return _synthesis_failed(
            "llm_unavailable", str(e), enquiry, classification,
            specialist_outputs, specialist_errors, retry_log,
            detail=None, config=config)
    except LLMError as e:
        status = "llm_invalid_output" if e.kind == "invalid_output" else "llm_unavailable"
        return _synthesis_failed(
            status, str(e), enquiry, classification,
            specialist_outputs, specialist_errors, retry_log,
            detail=e.detail, config=config)

    verification = {
        "config_present": bool(config),
        "proposal_non_empty": bool(proposal.get("body")),
        "missing_information_identified": bool(classification.get("missing_information")),
        "agents_selected": classification.get("selected_agents"),
        "specialist_errors": specialist_errors,
        "retry_log": retry_log,
    }

    proposal_id = proposal.get("proposal_id")
    approval_record = None
    request_id = None
    telegram_prompt = None
    prompt_delivered = None
    document_delivery = None
    if approval_mode:
        approval_record = request_approval(proposal, enquiry)
        request_id = approval_record.get("request_id")
        try:
            from orchestrator.telegram_approval import send_approval_prompt
            telegram_prompt = send_approval_prompt(request_id, proposal)
        except Exception as e:
            telegram_prompt = {"sent": False, "reason": f"send_approval_prompt raised: {e}"}
        prompt_delivered = bool((telegram_prompt or {}).get("sent"))

        # Two messages, deliberately. The prompt is the approval boundary and
        # keeps the gateway's delivery vocabulary; the PDF is an attachment the
        # gateway cannot carry and goes via the Bot API with its own vocabulary.
        # A PDF that fails must never make a delivered prompt look undelivered,
        # so this result is recorded separately and never feeds prompt_delivered.
        document_delivery = _deliver_proposal_document(proposal, config)

    external_action = {
        "type": "send_proposal",
        "request_id": request_id,
        "proposal_id": proposal_id,
        "status": "pending_approval" if approval_mode else "ready",
        "requires_approval": True,
        "idempotency_key": request_id,
        "retry_safe": True,
        "telegram_prompt": telegram_prompt,
        "document_delivery": document_delivery,
    }
    external_actions.append(external_action)

    status = "awaiting_approval"
    if not approval_mode:
        status = "ready_for_approval"
    elif not prompt_delivered:
        # A prompt nobody received must not look like one that was delivered.
        status = "approval_prompt_undelivered"
    if not classification["selected_agents"] and specialist_errors:
        status = "error"

    return {
        "workflow": "flagship",
        "version": "1.0.0",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "enquiry": enquiry,
        "classification": classification,
        "agents_invoked": classification["selected_agents"],
        "specialist_outputs": specialist_outputs,
        "proposal": proposal,
        "verification": verification,
        "requires_approval": True,
        "approval_prompt_delivered": prompt_delivered,
        "approval_request": approval_record,
        "external_actions": external_actions,
        "status": status,
        "retry_log": retry_log,
        "specialist_errors": specialist_errors,
        "recovery": {
            "resume_with": "Use approval.request_id with resume_if_approved(request_id) after human decision.",
            "do_not_retry_external_action_automatically": True,
            "telegram_commands": [
                f"APPROVE {request_id}",
                f"REJECT {request_id}",
            ] if request_id else [],
        },
    }


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
