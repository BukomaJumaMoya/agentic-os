#!/usr/bin/env python3
"""
Flagship End-to-End Workflow — client enquiry → proposal → approval → action.

This module implements the bounded flagship workflow for the freelance
engineering agentic stack. It is a deterministic pipeline, not an
open-ended autonomous loop.
"""

import json
import sys
import os
import uuid
from pathlib import Path
from datetime import datetime, timezone

BASE = Path(__file__).resolve().parent.parent
APPROVAL_DIR = BASE / ".approval"
EVIDENCE_DIR = BASE / "evidence"
CONFIG_PATH = BASE / "config" / "juma.json"


def load_config():
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text())
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

    selected_agents = []
    if any(hits["research"]):
        selected_agents.append("research")
    if any(hits["projects"]):
        selected_agents.append("projects")
    if any(hits["coding"]):
        selected_agents.append("coding")
    if not selected_agents:
        selected_agents = ["research"]

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


def synthesize_proposal(enquiry: str, research_findings: dict, project_context: dict, coding_context: dict, config: dict) -> dict:
    proposal_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    name = config.get("name", "Juma Moya")
    business = config.get("business", "Bukoma Freelance Software Engineering")
    services = config.get("services", [])
    default_rate = config.get("default_rate", "$75–150/hr")
    sla = config.get("proposal_sla", "48 hours")

    subject = f"Re: {enquiry[:80]}"
    body = f"""Hi,

Thank you for reaching out.

Based on your enquiry, here is an initial assessment:

Service fit:
{chr(10).join('- ' + s for s in services[:3])}

Next steps:
1. Clarify scope, timeline, and budget.
2. Send a fixed-price or T&M proposal.
3. Schedule a 30-minute alignment call.

Response SLA: {sla}

Regards,
{name}
{business}
"""

    return {
        "proposal_id": proposal_id,
        "created_at": now,
        "subject": subject,
        "body": body,
        "config_used": {
            "name": name,
            "business": business,
            "default_rate": default_rate,
        },
        "evidence": {
            "research_findings": research_findings,
            "project_context": project_context,
            "coding_context": coding_context,
        },
    }


def request_approval(proposal: dict, enquiry: str) -> dict:
    APPROVAL_DIR.mkdir(exist_ok=True)
    EVIDENCE_DIR.mkdir(exist_ok=True)
    request_id = proposal.get("proposal_id") or str(uuid.uuid4())
    record = {
        "request_id": request_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "enquiry": enquiry,
        "proposal": proposal,
        "status": "pending",
    }
    request_path = APPROVAL_DIR / f"{request_id}.request.json"
    decision_path = APPROVAL_DIR / f"{request_id}.decision.json"
    # idempotent write: do not overwrite existing request
    if not request_path.exists():
        request_path.write_text(json.dumps(record, indent=2))
    evidence_path = EVIDENCE_DIR / f"{request_id}-proposal.json"
    if not evidence_path.exists():
        evidence_path.write_text(json.dumps(proposal, indent=2))
    return record


def _exec_with_retry(name, payload, retries=2):
    from orchestrator.orchestrator import invoke
    output, error = invoke(name, payload, retries=retries)
    return output, error


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
    project_context = {}
    coding_context = {}
    specialist_errors = []
    external_actions = []
    retry_log = []

    for agent in classification["selected_agents"]:
        payload = {}
        if agent == "research":
            payload = {"query": enquiry[:200], "max_sources": 3, "fetch_content": True}
        elif agent == "projects":
            payload = {"action": "search_tasks", "query": enquiry[:100]}
        elif agent == "coding":
            payload = {"action": "explain", "code": enquiry[:200], "language": "python"}

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
            research_findings = output
            if research_findings.get("status") == "no_results":
                msg = "Research returned no results"
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
                    "status": "fallback_no_results"
                }
        elif agent == "projects":
            project_context = output
        elif agent == "coding":
            coding_context = output

    proposal = synthesize_proposal(enquiry, research_findings, project_context, coding_context, config)

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
    if approval_mode:
        approval_record = request_approval(proposal, enquiry)
        request_id = approval_record.get("request_id")

    external_action = {
        "type": "send_proposal",
        "request_id": request_id,
        "proposal_id": proposal_id,
        "status": "pending_approval" if approval_mode else "ready",
        "requires_approval": True,
        "idempotency_key": request_id,
        "retry_safe": True,
    }
    external_actions.append(external_action)

    status = "awaiting_approval"
    if not approval_mode:
        status = "ready_for_approval"
    if not classification["selected_agents"] and specialist_errors:
        status = "error"

    return {
        "workflow": "flagship",
        "version": "1.0.0",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "enquiry": enquiry,
        "classification": classification,
        "agents_invoked": classification["selected_agents"],
        "specialist_outputs": {
            "research": research_findings,
            "projects": project_context,
            "coding": coding_context,
        },
        "proposal": proposal,
        "verification": verification,
        "requires_approval": True,
        "approval_request": approval_record,
        "external_actions": external_actions,
        "status": status,
        "retry_log": retry_log,
        "specialist_errors": specialist_errors,
        "recovery": {
            "resume_with": "Use approval.request_id with resume_if_approved(request_id) after human decision.",
            "do_not_retry_external_action_automatically": True,
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
