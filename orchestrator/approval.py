#!/usr/bin/env python3
"""
Human Approval Boundary — explicit enforcement between internal work and
external actions.

Authority levels:
- READ
- INTERNAL_WRITE
- EXTERNAL_ACTION
"""

import json
import os
import sys
import time
import uuid
from pathlib import Path
from datetime import datetime, timezone

BASE = Path(__file__).resolve().parent.parent
APPROVAL_DIR = BASE / ".approval"

SAFE_POLICIES = {
    # Actions explicitly designated safe without approval
    "read", "list", "get", "search", "query", "explain", "analyze"
}

AUTHORITY_MAP = {
    # Research agent
    "research": "READ",
    # Projects agent
    "list_spaces": "READ",
    "search_tasks": "READ",
    "get_task": "READ",
    "list_lists": "READ",
    "list_folders": "READ",
    "create_task": "INTERNAL_WRITE",
    "update_task": "INTERNAL_WRITE",
    # Coding agent
    "explain": "READ",
    "review": "READ",
    "generate": "INTERNAL_WRITE",
    "debug": "INTERNAL_WRITE",
    "refactor": "INTERNAL_WRITE",
    "test": "INTERNAL_WRITE",
}


def classify(action: str) -> str:
    if not action:
        return "READ"
    a = action.lower()
    if a in SAFE_POLICIES:
        return "READ"
    mapped = AUTHORITY_MAP.get(a)
    if mapped:
        return mapped
    # Heuristic
    if any(k in a for k in ["create", "update", "write", "generate", "send", "publish", "submit", "deploy", "approve"]):
        return "EXTERNAL_ACTION"
    return "READ"


def _request_path(request_id: str) -> Path:
    return APPROVAL_DIR / f"{request_id}.request.json"


def _decision_path(request_id: str) -> Path:
    return APPROVAL_DIR / f"{request_id}.decision.json"


def request_approval(proposal: dict) -> dict:
    """Create an approval request and return the request record."""
    APPROVAL_DIR.mkdir(exist_ok=True)
    request_id = proposal.get("request_id") or str(uuid.uuid4())
    record = {
        "request_id": request_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "proposal": proposal,
        "status": "pending",
        "expires_at": datetime.now(timezone.utc).isoformat(),  # simplified
    }
    _request_path(request_id).write_text(json.dumps(record, indent=2))
    return record


def wait_for_decision(request_id: str, timeout: int = 300) -> dict:
    """
    Poll for approval decision.
    
    Returns decision dict with keys:
      - approved: bool
      - approver: str | None
      - timestamp: str | None
      - reason: str | None
    """
    deadline = time.time() + timeout
    decision_file = _decision_path(request_id)
    while time.time() < deadline:
        if decision_file.exists():
            try:
                return json.loads(decision_file.read_text())
            except Exception:
                pass
        time.sleep(0.5)
    return {"approved": False, "reason": "timeout", "timestamp": None, "approver": None}


def record_decision(request_id: str, approved: bool, approver: str = None, reason: str = None, overwrite: bool = False):
    decision_path = _decision_path(request_id)
    if decision_path.exists() and not overwrite:
        try:
            existing = json.loads(decision_path.read_text())
            return existing
        except Exception:
            pass
    decision = {
        "request_id": request_id,
        "approved": approved,
        "approver": approver or "human",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "reason": reason or ("approved" if approved else "rejected"),
    }
    decision_path.write_text(json.dumps(decision, indent=2))
    return decision


def is_approved(request_id: str) -> bool:
    decision_file = _decision_path(request_id)
    if not decision_file.exists():
        return False
    try:
        decision = json.loads(decision_file.read_text())
        return decision.get("approved") is True
    except Exception:
        return False


def enforce(step: dict, task_context: dict = None) -> dict:
    """
    Enforce approval boundary for a step.
    
    Returns:
      - {"allowed": True, "authority": "READ|INTERNAL_WRITE", "executed": True}
      - {"allowed": True, "authority": "EXTERNAL_ACTION", "executed": False, "request_id": ...}
      - {"allowed": False, "reason": ..., "request_id": ...}
    """
    action = step.get("action") or step.get("step") or "read"
    authority = classify(action)
    
    if authority == "READ":
        return {"allowed": True, "authority": authority, "executed": True}
    
    if authority == "INTERNAL_WRITE":
        return {"allowed": True, "authority": authority, "executed": True}
    
    # EXTERNAL ACTION
    proposal = {
        "request_id": step.get("request_id"),
        "task": (task_context or {}).get("task", ""),
        "action": action,
        "agent": step.get("agent"),
        "description": step.get("description") or f"External action: {action} via {step.get('agent')}",
        "authority": authority,
        "payload": step.get("payload"),
    }
    record = request_approval(proposal)
    return {
        "allowed": True,
        "authority": authority,
        "executed": False,
        "request_id": record["request_id"],
        "status": "awaiting_approval",
        "proposal": proposal,
    }


def resume_if_approved(request_id: str) -> dict:
    """
    Check if an external action was approved and is ready for execution.
    
    Returns:
      - {"approved": True, "executed": True}
      - {"approved": False, "reason": "rejected|timeout|pending"}
    """
    if not request_id:
        return {"approved": False, "reason": "missing_request_id"}
    decision_file = _decision_path(request_id)
    if not decision_file.exists():
        return {"approved": False, "reason": "pending"}
    try:
        decision = json.loads(decision_file.read_text())
        if decision.get("approved") is True:
            return {"approved": True, "executed": True, "decision": decision}
        return {"approved": False, "reason": decision.get("reason", "rejected"), "decision": decision}
    except Exception as e:
        return {"approved": False, "reason": f"invalid_decision: {e}"}


def cleanup(request_id: str):
    for p in [_request_path(request_id), _decision_path(request_id)]:
        if p.exists():
            p.unlink()
