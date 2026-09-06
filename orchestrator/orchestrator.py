#!/usr/bin/env python3
"""
Agentic Orchestrator — bounded decision-making for specialist selection,
execution, verification, and approval interception.

Authority boundary:
- READ
- INTERNAL_WRITE
- EXTERNAL_ACTION  <- orchestrator must NOT execute these without approval
"""

import sys
import json
import subprocess
import os
from pathlib import Path

AGENT = "orchestrator"
VERSION = "1.0.0"

BASE = Path(__file__).resolve().parent.parent
APPROVAL_DIR = BASE / ".approval"

AGENTS = {
    "research": {
        "path": BASE / "agents" / "research" / "main.py",
        "authority": ["READ"],
        "keywords": ["research", "find", "search", "compare", "competitor", "technology", "summarize"]
    },
    "projects": {
        "path": BASE / "agents" / "projects" / "main.js",
        "authority": ["READ", "INTERNAL_WRITE"],
        "keywords": ["clickup", "task", "project", "list", "create task", "update task", "status"]
    },
    "coding": {
        "path": BASE / "agents" / "coding" / "main.py",
        "authority": ["READ", "INTERNAL_WRITE"],
        "keywords": ["code", "generate", "debug", "review", "refactor", "explain", "test", "python", "javascript"]
    }
}

EXTERNAL_ACTIONS = {
    "send_message", "external_action", "publish", "submit", "deploy", "approve"
}


def classify(task):
    t = task.lower()
    scores = {}
    for name, cfg in AGENTS.items():
        score = sum(1 for kw in cfg["keywords"] if kw in t)
        scores[name] = score
    best = max(scores, key=scores.get)
    if scores[best] == 0:
        return "general", []
    selected = [best]
    if scores.get("research", 0) > 0 and "research" not in selected:
        selected.append("research")
    if scores.get("coding", 0) > 0 and "coding" not in selected:
        selected.append("coding")
    return "specialist", selected


def needs_research(task):
    t = task.lower()
    return any(kw in t for kw in AGENTS["research"]["keywords"])


def needs_project(task):
    t = task.lower()
    return any(kw in t for kw in AGENTS["projects"]["keywords"])


def needs_coding(task):
    t = task.lower()
    return any(kw in t for kw in AGENTS["coding"]["keywords"])


def invoke(agent_name, payload, retries=2):
    from orchestrator.approval import resume_if_approved
    if agent_name not in AGENTS:
        return None, f"Rejected unknown agent: {agent_name}"
    cfg = AGENTS[agent_name]
    path = cfg["path"]
    cmd = [sys.executable, str(path)] if path.suffix == ".py" else ["node", str(path)]
    attempt = 0
    last = None
    while attempt < retries:
        attempt += 1
        try:
            proc = subprocess.run(
                cmd,
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                timeout=120
            )
            if proc.returncode != 0:
                last = proc.stderr.strip() or proc.stdout.strip()
                continue
            try:
                return json.loads(proc.stdout), None
            except Exception as e:
                return None, f"Invalid JSON from {agent_name}: {e}"
        except Exception as e:
            last = str(e)
    return None, last or f"{agent_name} unavailable after {retries} attempts"


def verify(agent_name, output, expected_keys):
    if not isinstance(output, dict):
        return False, "Output is not a JSON object"
    if output.get("agent") != agent_name:
        return False, f"Agent name mismatch: {output.get('agent')}"
    missing = [k for k in expected_keys if k not in output]
    if missing:
        return False, f"Missing keys: {missing}"
    return True, "OK"


def build_plan(task):
    category, agents = classify(task)
    plan = {
        "task": task,
        "category": category,
        "agents": agents,
        "steps": [],
        "constraints": ["no external actions without approval", "bounded retries", "verify each step"],
        "unknowns": []
    }
    if category == "general":
        plan["unknowns"].append("Task does not clearly map to a specialist agent")
        plan["steps"].append({"step": "ask_user", "reason": "insufficient information to classify"})
        return plan

    if needs_research(task):
        plan["steps"].append({"step": "research", "agent": "research", "authority": "READ"})
    if needs_project(task):
        plan["steps"].append({"step": "projects", "agent": "projects", "authority": "READ | INTERNAL_WRITE"})
    if needs_coding(task):
        plan["steps"].append({"step": "coding", "agent": "coding", "authority": "READ | INTERNAL_WRITE"})
    return plan


def execute_plan(plan, approval_mode=True):
    from orchestrator.approval import enforce, resume_if_approved
    executions = []
    pending_approval = {}
    for step in plan.get("steps", []):
        if step.get("step") == "ask_user":
            executions.append({
                "step": "ask_user",
                "status": "blocked",
                "reason": "insufficient information",
                "output": None
            })
            continue
        agent_name = step.get("agent")
        payload = {"task": plan["task"]}
        if agent_name == "research":
            payload.update({"query": plan["task"], "max_sources": 3, "fetch_content": True})
        elif agent_name == "projects":
            payload.update({"action": "search_tasks", "query": plan["task"][:100]})
        elif agent_name == "coding":
            payload.update({"action": "generate", "prompt": plan["task"], "language": "python"})
        enforcement = enforce(step, {"task": plan["task"]})
        if enforcement.get("status") == "awaiting_approval":
            request_id = enforcement.get("request_id")
            pending_approval[request_id] = {"step": step, "payload": payload, "agent_name": agent_name}
            executions.append({
                "step": step.get("step"),
                "agent": agent_name,
                "status": "awaiting_approval",
                "request_id": request_id,
                "proposal": enforcement.get("proposal"),
                "output": None
            })
            continue
        output, error = invoke(agent_name, payload)
        if error:
            executions.append({
                "step": step.get("step"),
                "agent": agent_name,
                "status": "error",
                "error": error,
                "output": None
            })
            continue
        expected = ["agent", "version", "action", "authority", "status"]
        if agent_name == "research":
            expected = ["agent", "query", "findings", "status"]
        ok, msg = verify(agent_name, output, expected)
        executions.append({
            "step": step.get("step"),
            "agent": agent_name,
            "status": "success" if ok else "verification_failed",
            "verification": msg,
            "output": output if ok else None
        })
    return executions, pending_approval


def detect_external_actions(executions):
    actions = []
    for ex in executions:
        if ex.get("status") != "success":
            continue
        output = ex.get("output") or {}
        for key in output:
            if key in EXTERNAL_ACTIONS:
                actions.append({"step": ex.get("step"), "action": key, "agent": ex.get("agent")})
    return actions


def orchestrate(task, approval_mode=True):
    plan = build_plan(task)
    executions, pending_approval = execute_plan(plan, approval_mode=approval_mode)
    external = detect_external_actions(executions)
    success = all(ex.get("status") == "success" for ex in executions)
    result = {
        "orchestrator": AGENT,
        "version": VERSION,
        "task": task,
        "plan": plan,
        "executions": executions,
        "external_actions_detected": external,
        "requires_approval": bool(external) or bool(pending_approval),
        "status": "success" if success else "partial_failure",
        "pending_approval": pending_approval,
    }
    return result


def main():
    try:
        data = json.load(sys.stdin)
    except Exception as e:
        print(json.dumps({"error": f"Invalid input JSON: {e}"}))
        sys.exit(1)
    task = data.get("task")
    if not task:
        print(json.dumps({"error": "Missing required field: task"}))
        sys.exit(1)
    approval_mode = data.get("approval_mode", True)
    result = orchestrate(task, approval_mode=approval_mode)
    print(json.dumps(result, indent=2))
    sys.exit(0 if result["status"] == "success" else 2)


if __name__ == "__main__":
    main()
