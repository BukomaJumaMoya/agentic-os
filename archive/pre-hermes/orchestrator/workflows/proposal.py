#!/usr/bin/env python3
"""
The enquiry -> proposal workflow.

Moved verbatim from flagship.run_workflow. The body below is the same code that
ran before the registry existed, with one mechanical change: calls to helpers
that still live in flagship are qualified as flagship.<name> so they resolve
through the module at call time. Four test suites reassign
flagship._exec_with_retry to keep specialist agents offline, and a bound-at-
import reference would silently ignore them.

The envelope still reports "workflow": "flagship". That value predates the
registry and two suites assert it; keeping it is how identity is proven without
editing the tests that prove it. workflow_name carries the registered name
alongside it, and the legacy field is scheduled for removal once the move has
been verified.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from datetime import datetime, timezone  # noqa: E402

from orchestrator import flagship  # noqa: E402
from orchestrator.approval import ConfigError  # noqa: E402
from orchestrator.llm import LLMError  # noqa: E402
from orchestrator.research_query import extract_queries  # noqa: E402
from orchestrator import jobs  # noqa: E402
from orchestrator.workflow import Workflow, register  # noqa: E402

MAX_ENQUIRY_CHARS = 8000

# Shape only. This workflow's body already validates its own inputs -- an empty
# enquiry returns status "error" with "Missing enquiry text", and an oversized
# one is refused further down at the model boundary. A "required" or a
# "maxLength" here would intercept those first and rewrite the status the
# caller sees, which is a behaviour change wearing the costume of a schema.
#
# The registry's validation is therefore close to a no-op for this workflow, by
# design: the move must be observably identical. Workflows written against the
# registry will use it fully.
INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "enquiry": {"type": "string"},
        "approval_mode": {"type": "boolean"},
        "force_agent": {"type": "string"},
    },
}


def _run(enquiry: str, approval_mode: bool = True, force_agent: str = None,
         job: str = None) -> dict:
    """The enquiry pipeline.

    `job` is optional. Without one every checkpoint is a no-op and this behaves
    exactly as it did before jobs existed, which is what keeps the registry
    move's identity proof re-runnable.
    """
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

    classification = flagship.classify_enquiry(enquiry)
    config = flagship.load_config()

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
        # Before each agent: the cheapest place to stop, because nothing has
        # been spent on this one yet.
        jobs.checkpoint(job, f"before:{agent}")
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
        output, error = flagship._exec_with_retry(agent, payload, retries=2)
        # After each agent: an agent already running is never interrupted, so
        # this is the first moment its cost is known to be spent.
        jobs.checkpoint(job, f"after:{agent}",
                        "failed" if error else "ok")
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
    # Before synthesis: the last point before a paid model call.
    jobs.checkpoint(job, "before:synthesis")

    try:
        proposal = flagship.synthesize_proposal(
            enquiry, research_findings, project_context, coding_context, config)
    except ConfigError as e:
        return flagship._synthesis_failed(
            "llm_unavailable", str(e), enquiry, classification,
            specialist_outputs, specialist_errors, retry_log,
            detail=None, config=config)
    except LLMError as e:
        status = "llm_invalid_output" if e.kind == "invalid_output" else "llm_unavailable"
        return flagship._synthesis_failed(
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
        # Before filing the approval request: the last point at which nothing
        # is waiting for a human and nothing has been offered for sending.
        jobs.checkpoint(job, "before:approval_request")
        approval_record = flagship.request_approval(proposal, enquiry)
        # Linked immediately, not at the end, so a cancellation arriving while
        # this job still runs can consult the approval state.
        if job:
            jobs.attach_result_ref(
                job, {"request_id": approval_record.get("request_id"),
                      "artifact": "proposal"})
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
        document_delivery = flagship._deliver_proposal_document(proposal, config)

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

def _entry(inputs: dict, **options):
    """Registry entry point: unpack declared inputs and call the moved body."""
    return _run(
        inputs.get("enquiry") or "",
        approval_mode=options.get("approval_mode",
                                  inputs.get("approval_mode", True)),
        force_agent=options.get("force_agent", inputs.get("force_agent")),
        job=options.get("job"),
    )


PROPOSAL = register(Workflow(
    name="proposal",
    description="Draft a client proposal from an inbound enquiry.",
    input_schema=INPUT_SCHEMA,
    agents=("research", "projects", "coding"),
    artifact="proposal",
    requires_approval=True,
    run=_entry,
    # The statuses that mean the pipeline completed. A run ending in one of
    # these without a proposal is an artifact_missing failure, not a success.
    success_statuses=("awaiting_approval", "approval_prompt_undelivered",
                      "ready_for_approval"),
    max_runtime_seconds=300,
    legacy_workflow_field="flagship",
))
