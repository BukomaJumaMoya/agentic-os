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
import time
import uuid
from pathlib import Path
from datetime import datetime, timedelta, timezone

BASE = Path(__file__).resolve().parent.parent
# Single knob for relocating runtime state out of the repo tree. Defaults to
# the current in-repo location so existing callers and tests are unaffected.
STATE_ROOT = Path(os.getenv("AGENTIC_STATE_DIR") or BASE).resolve()
APPROVAL_DIR = STATE_ROOT / ".approval"
EVIDENCE_DIR = STATE_ROOT / "evidence"
CONFIG_PATH = BASE / "config" / "juma.json"


class ConfigError(RuntimeError):
    """Required operator configuration is missing."""


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        # Explicit: the file is UTF-8, but read_text() defaults to the locale
        # encoding (cp1252 on Windows), which mojibakes the en dash in
        # default_rate into every config_used block and evidence record.
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _config_value(env_var: str, *config_path: str):
    """Environment first, then a dotted path in config/juma.json."""
    v = os.getenv(env_var)
    if v and v.strip():
        return v.strip()
    node = load_config()
    for key in config_path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    if isinstance(node, (str, int)) and str(node).strip():
        return str(node).strip()
    return None


def allowed_user_id() -> str:
    """Telegram user id permitted to approve.

    No baked-in default: publishing the sole authorised chat id in source is
    audit S-4, and a silent fallback would let a misconfigured deployment
    authorise the wrong account.
    """
    v = _config_value("OPENCLAW_ALLOWED_USER_ID", "telegram", "allowed_user_id")
    if not v:
        raise ConfigError(
            "OPENCLAW_ALLOWED_USER_ID is not set and config/juma.json has no "
            "telegram.allowed_user_id; refusing to fall back to a baked-in id"
        )
    return v


def gateway_url() -> str:
    return _config_value("OPENCLAW_GATEWAY_URL", "openclaw", "gateway_url") \
        or "http://127.0.0.1:18789"


def gateway_token() -> str:
    """Shared secret for the OpenClaw gateway (gateway.auth.mode: token)."""
    v = _config_value("OPENCLAW_GATEWAY_TOKEN", "openclaw", "gateway_token")
    if not v:
        raise ConfigError(
            "OPENCLAW_GATEWAY_TOKEN is not set; the OpenClaw gateway runs with "
            "gateway.auth.mode=token and will reject unauthenticated requests"
        )
    return v

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
    # Writing to a third party's SaaS account is an external action, whatever
    # it is internal to. Reclassified per audit F-1.
    "create_task": "EXTERNAL_ACTION",
    "update_task": "EXTERNAL_ACTION",
    # Coding agent
    "explain": "READ",
    "review": "READ",
    "generate": "INTERNAL_WRITE",
    "debug": "INTERNAL_WRITE",
    "refactor": "INTERNAL_WRITE",
    "test": "INTERNAL_WRITE",
}


def classify(action: str) -> str:
    """Classify an action, failing closed.

    Only explicitly registered actions get a reduced authority. Anything else
    -- including an empty action and any action added in future -- is
    EXTERNAL_ACTION and therefore requires approval. The previous substring
    heuristic defaulted to READ, so 'wire_transfer' and 'pay_invoice' ran
    ungated (audit F-1).
    """
    if not action:
        return "EXTERNAL_ACTION"
    a = action.lower().strip()
    if a in SAFE_POLICIES:
        return "READ"
    return AUTHORITY_MAP.get(a, "EXTERNAL_ACTION")


# Audit F-5. An approval used to be permanent and reusable: once a decision file
# said approved, it authorised an action today, tomorrow, and every time anyone
# asked again. Two separate holes -- no expiry, and no consumption.
#
# A human approving "send this proposal" is approving it NOW, on the information
# in front of them. Fifteen minutes later that intent is stale; a day later it is
# not consent to anything. And consent given once is consent for one action, not
# a standing authorisation.
DEFAULT_DECISION_TTL_SECONDS = 900        # 15 minutes
DEFAULT_REQUEST_TTL_SECONDS = 24 * 3600   # a request nobody answered in a day


def _now():
    """Current UTC time.

    A function, not a direct call, so tests can advance the clock instead of
    sleeping. Expiry tested with real sleeps is a test that is either slow or
    lying about what it covers.
    """
    return datetime.now(timezone.utc)


def _ttl(env_var: str, config_key: str, default: int) -> int:
    raw = _config_value(env_var, "approval", config_key)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def decision_ttl_seconds() -> int:
    return _ttl("APPROVAL_DECISION_TTL_SECONDS", "decision_ttl_seconds",
                DEFAULT_DECISION_TTL_SECONDS)


def request_ttl_seconds() -> int:
    return _ttl("APPROVAL_REQUEST_TTL_SECONDS", "request_ttl_seconds",
                DEFAULT_REQUEST_TTL_SECONDS)


def _parse_time(value):
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    # A naive timestamp from an older record is treated as UTC rather than
    # crashing the comparison.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _consumed_path(request_id: str) -> Path:
    return APPROVAL_DIR / f"{request_id}.consumed.json"


def _request_path(request_id: str) -> Path:
    return APPROVAL_DIR / f"{request_id}.request.json"


def _decision_path(request_id: str) -> Path:
    return APPROVAL_DIR / f"{request_id}.decision.json"


# Public aliases: every module must derive approval-state paths from here so
# the AGENTIC_STATE_DIR knob and the file-naming convention have one owner.
def request_path(request_id: str) -> Path:
    return _request_path(request_id)


def decision_path(request_id: str) -> Path:
    return _decision_path(request_id)


def consumed_path(request_id: str) -> Path:
    return _consumed_path(request_id)


def request_approval(proposal: dict, enquiry: str | None = None) -> dict:
    """Create an approval request and return the request record.

    ``enquiry`` is optional so the flagship workflow can record the originating
    client message alongside the proposal and emit the matching evidence file.
    When it is omitted the behaviour is the plain approval-boundary request.
    Writes are idempotent: an existing request or evidence file for the same
    id is never overwritten, so a retry cannot clobber a pending decision.
    """
    APPROVAL_DIR.mkdir(exist_ok=True)
    request_id = (
        proposal.get("request_id")
        or proposal.get("proposal_id")
        or str(uuid.uuid4())
    )
    created_at = _now()
    record = {
        "request_id": request_id,
        "timestamp": created_at.isoformat(),
        "proposal": proposal,
        "status": "pending",
        # Was `datetime.now()` with a "# simplified" comment -- a field that
        # said every request expired the instant it was created, and which
        # nothing checked. Now it means what it says and record_decision
        # enforces it.
        "expires_at": (created_at + timedelta(seconds=request_ttl_seconds())).isoformat(),
        "request_ttl_seconds": request_ttl_seconds(),
    }
    if enquiry is not None:
        record["enquiry"] = enquiry
        EVIDENCE_DIR.mkdir(exist_ok=True)
        evidence_path = EVIDENCE_DIR / f"{request_id}-proposal.json"
        if not evidence_path.exists():
            evidence_path.write_text(json.dumps(proposal, indent=2))
    request_path = _request_path(request_id)
    if not request_path.exists():
        request_path.write_text(json.dumps(record, indent=2))
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


def record_decision(request_id: str, approved: bool, approver: str = None, reason: str = None, overwrite: bool = False, channel: str = None):
    path = _decision_path(request_id)
    if path.exists() and not overwrite:
        try:
            existing = json.loads(path.read_text())
            return existing
        except Exception:
            pass
    decided_at = _now()

    # Approving a request that went stale days ago is not consent to act on it.
    # A rejection is always allowed: refusing something old is still meaningful.
    if approved:
        request_file = _request_path(request_id)
        if request_file.exists():
            try:
                record = json.loads(request_file.read_text())
            except Exception:
                record = {}
            request_expiry = _parse_time(record.get("expires_at"))
            if request_expiry and decided_at > request_expiry:
                return {
                    "request_id": request_id,
                    "approved": False,
                    "approver": approver or "human",
                    "timestamp": decided_at.isoformat(),
                    "reason": "request_expired",
                    "recorded": False,
                    "request_expired_at": record.get("expires_at"),
                }

    ttl = decision_ttl_seconds()
    decision = {
        "request_id": request_id,
        "approved": approved,
        "approver": approver or "human",
        "timestamp": decided_at.isoformat(),
        "reason": reason or ("approved" if approved else "rejected"),
        # Only an approval carries a deadline. A rejection does not go stale
        # into permission.
        "expires_at": (decided_at + timedelta(seconds=ttl)).isoformat() if approved else None,
        "ttl_seconds": ttl if approved else None,
    }
    if channel:
        decision["channel"] = channel
    APPROVAL_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(decision, indent=2))
    return decision


def decision_state(request_id: str) -> dict:
    """Resolve what a request's approval actually permits, right now.

    One place decides what "approved" means, so is_approved, resume_if_approved
    and the Telegram commands can never disagree.

    state is one of:
      pending   no decision recorded yet
      approved  approved, unexpired, unused -- the only state that permits action
      rejected  explicitly refused; permanent
      expired   approved, but the approval has aged out
      consumed  approved and already used for an action
      invalid   the decision file exists but could not be read
    """
    consumed = _consumed_path(request_id)
    if consumed.exists():
        record = {}
        try:
            record = json.loads(consumed.read_text())
        except Exception:
            pass
        return {"state": "consumed", "decision": record,
                "consumed_at": record.get("consumed_at")}

    decision_file = _decision_path(request_id)
    if not decision_file.exists():
        return {"state": "pending", "decision": None}
    try:
        decision = json.loads(decision_file.read_text())
    except Exception as e:
        return {"state": "invalid", "decision": None, "detail": str(e)}

    if decision.get("approved") is not True:
        return {"state": "rejected", "decision": decision}

    expires_at = _parse_time(decision.get("expires_at"))
    if expires_at is None:
        # A decision written before TTLs existed. Age it from its own timestamp
        # rather than treating a missing deadline as "never expires", which is
        # the behaviour this change exists to remove.
        decided = _parse_time(decision.get("timestamp"))
        if decided is not None:
            expires_at = decided + timedelta(seconds=decision_ttl_seconds())

    now = _now()
    if expires_at is not None and now > expires_at:
        return {"state": "expired", "decision": decision,
                "expires_at": expires_at.isoformat(),
                "expired_for_seconds": int((now - expires_at).total_seconds())}

    remaining = int((expires_at - now).total_seconds()) if expires_at else None
    return {"state": "approved", "decision": decision,
            "expires_at": expires_at.isoformat() if expires_at else None,
            "seconds_remaining": remaining}


def consume_decision(request_id: str) -> dict:
    """Mark an approval used, so it cannot authorise a second action.

    The rename is atomic: os.replace either moves the decision file or raises.
    Two processes racing to consume the same approval cannot both succeed --
    the loser finds no source file and is told it was already used.
    """
    source = _decision_path(request_id)
    target = _consumed_path(request_id)
    if not source.exists():
        state = decision_state(request_id)
        return {"consumed": False,
                "reason": "already_consumed" if state["state"] == "consumed" else "no_decision",
                "state": state["state"]}
    try:
        record = json.loads(source.read_text())
    except Exception:
        record = {"request_id": request_id}
    record["consumed_at"] = _now().isoformat()
    try:
        source.write_text(json.dumps(record, indent=2))
        os.replace(source, target)
    except FileNotFoundError:
        return {"consumed": False, "reason": "already_consumed", "state": "consumed"}
    except Exception as e:
        return {"consumed": False, "reason": f"consume_failed: {e}", "state": "unknown"}
    return {"consumed": True, "consumed_at": record["consumed_at"], "state": "consumed"}


def is_approved(request_id: str) -> bool:
    """True only when an approval is valid, unexpired and unused."""
    return decision_state(request_id)["state"] == "approved"


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

    state = decision_state(request_id)
    decision = state.get("decision")

    if state["state"] == "approved":
        return {"approved": True, "executed": True, "reason": "approved",
                "decision": decision, "state": "approved",
                "expires_at": state.get("expires_at"),
                "seconds_remaining": state.get("seconds_remaining")}

    # Every refusal keeps its own reason. "expired" and "already used" are not
    # the same as "pending" and not the same as "rejected", and a caller that
    # cannot tell them apart cannot tell the operator what to do next.
    out = {"approved": False, "state": state["state"], "decision": decision}
    if state["state"] == "expired":
        out["reason"] = "expired"
        out["expires_at"] = state.get("expires_at")
        out["expired_for_seconds"] = state.get("expired_for_seconds")
    elif state["state"] == "consumed":
        out["reason"] = "already_used"
        out["consumed_at"] = state.get("consumed_at")
    elif state["state"] == "rejected":
        out["reason"] = (decision or {}).get("reason") or "rejected"
    elif state["state"] == "invalid":
        out["reason"] = f"invalid_decision: {state.get('detail')}"
    else:
        out["reason"] = "pending"
    return out


def cleanup(request_id: str):
    """Remove every artifact for a request: request, decision, and evidence.

    The evidence file was previously left behind, so callers that expected a
    clean slate -- including every test suite's teardown -- silently
    accumulated proposal JSON.
    """
    for p in [
        _request_path(request_id),
        _decision_path(request_id),
        _consumed_path(request_id),
        EVIDENCE_DIR / f"{request_id}-proposal.json",
    ]:
        if p.exists():
            p.unlink()
