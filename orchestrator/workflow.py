#!/usr/bin/env python3
"""
Workflow registry and dispatch.

A workflow is a named, declared unit of work: what it accepts, which agents it
may invoke, what artifact it produces, and whether a human must approve the
result. Registering one is how a new capability enters this system; there is no
other entry point.

Why a registry rather than more functions
-----------------------------------------
flagship.run_workflow was a 286-line function that was simultaneously the
enquiry pipeline, the agent scheduler, the approval gate and the delivery path.
Adding a second kind of work meant either a second copy of all of that or a flag
threaded through it. Both are how the audit's findings happened in the first
place.

Declarations that do real work
------------------------------
Two fields are checked rather than documented:

  agents    the dispatcher verifies, after the run, that the workflow invoked
            only agents it declared. A workflow cannot quietly acquire a new
            external dependency.

  artifact  a workflow that declares it produces a proposal and finishes in a
            success status without one has FAILED. The recurring bug in this
            codebase is work that reports success having produced nothing.

Failures are returned as a status, never raised, matching the rest of the
orchestrator: a caller that cannot tell "no such workflow" from "bad input" from
"the model was unreachable" cannot tell the operator what to do next.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_REGISTRY = {}
_LOADED = False


class Workflow:
    """One registered unit of work.

    run(inputs, ctx) -> envelope dict. The envelope must carry at least
    "status"; the dispatcher adds the common fields around it.
    """

    def __init__(self, name, description, input_schema, agents, artifact,
                 requires_approval, run, success_statuses=(),
                 max_runtime_seconds=300, legacy_workflow_field=None):
        self.name = name
        self.description = description
        self.input_schema = input_schema
        self.agents = tuple(agents)
        self.artifact = artifact
        self.requires_approval = requires_approval
        self.run = run
        # Statuses that mean the work completed and the artifact should exist.
        self.success_statuses = tuple(success_statuses)
        self.max_runtime_seconds = max_runtime_seconds
        # Some envelopes carry a historical "workflow" value that predates the
        # registry. Keeping it is how identity with the existing suites is
        # proven without editing the tests that prove it.
        self.legacy_workflow_field = legacy_workflow_field

    def __repr__(self):
        return f"<Workflow {self.name}>"


def register(workflow: Workflow) -> Workflow:
    if workflow.name in _REGISTRY:
        raise ValueError(f"workflow already registered: {workflow.name}")
    _REGISTRY[workflow.name] = workflow
    return workflow


def _ensure_loaded():
    """Import the workflow package so registration happens.

    Done lazily and once: importing it at module level would make
    workflow -> workflows -> flagship -> workflow a cycle.
    """
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    import orchestrator.workflows  # noqa: F401


def get(name):
    _ensure_loaded()
    return _REGISTRY.get(name)


def names():
    _ensure_loaded()
    return sorted(_REGISTRY)


# ---------------------------------------------------------------------------
# Input validation.
#
# A deliberately small subset of JSON Schema -- type, required, properties,
# string minLength/maxLength -- implemented here rather than pulled in as a
# dependency. The repository has exactly one Python dependency and it earns its
# place by rendering PDFs; a schema validator for four keyword types does not.
# ---------------------------------------------------------------------------

_TYPES = {
    "object": dict, "array": list, "string": str,
    "number": (int, float), "integer": int, "boolean": bool,
}


def validate_inputs(schema: dict, inputs) -> list:
    """Return a list of violations; empty means acceptable."""
    problems = []
    if not isinstance(schema, dict):
        return problems

    expected = schema.get("type")
    if expected and expected in _TYPES and not isinstance(inputs, _TYPES[expected]):
        return [f"inputs must be {expected}, got {type(inputs).__name__}"]

    if not isinstance(inputs, dict):
        return problems

    for key in schema.get("required") or []:
        value = inputs.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            problems.append(f"missing required input: {key}")

    for key, spec in (schema.get("properties") or {}).items():
        if key not in inputs or inputs[key] is None:
            continue
        value = inputs[key]
        want = spec.get("type")
        if want and want in _TYPES and not isinstance(value, _TYPES[want]):
            problems.append(f"{key} must be {want}, got {type(value).__name__}")
            continue
        if isinstance(value, str):
            lo, hi = spec.get("minLength"), spec.get("maxLength")
            if lo is not None and len(value.strip()) < lo:
                problems.append(f"{key} is shorter than {lo} characters")
            if hi is not None and len(value) > hi:
                # Refuse, never truncate: the tail of an input is the part
                # someone bothered to write.
                problems.append(
                    f"{key} is {len(value)} characters, above the {hi} limit; "
                    "refusing rather than truncating")
    return problems


def _envelope(status, workflow_name, **extra):
    out = {
        "workflow_name": workflow_name,
        "status": status,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    out.update(extra)
    return out


def run(name: str, inputs: dict, **options) -> dict:
    """Dispatch to a registered workflow.

    options are passed through to the workflow's run function, so a workflow
    owns its own switches rather than the dispatcher growing a flag per caller.
    """
    _ensure_loaded()

    workflow = _REGISTRY.get(name)
    if workflow is None:
        return _envelope("unknown_workflow", name,
                         error=f"no workflow registered as {name!r}",
                         available=sorted(_REGISTRY))

    inputs = inputs if isinstance(inputs, dict) else {}
    violations = validate_inputs(workflow.input_schema, inputs)
    if violations:
        # Before any agent runs and before any model call: a malformed request
        # costs nothing.
        return _envelope("invalid_input", name,
                         error="; ".join(violations), violations=violations)

    result = workflow.run(inputs, **options)
    if not isinstance(result, dict):
        return _envelope("invalid_workflow_result", name,
                         error=f"{name} returned {type(result).__name__}, not a dict")

    status = result.get("status")

    # Declared agents are a boundary, not a comment.
    invoked = set(result.get("agents_invoked") or [])
    undeclared = sorted(invoked - set(workflow.agents))
    if undeclared:
        result["status"] = "authority_violation"
        result["error"] = (
            f"{name} invoked undeclared agent(s): {', '.join(undeclared)}")
        return result

    # A success that produced nothing is not a success.
    if workflow.artifact != "none" and status in workflow.success_statuses:
        if not result.get(workflow.artifact):
            result["status"] = "artifact_missing"
            result["error"] = (
                f"{name} finished with status {status!r} but produced no "
                f"{workflow.artifact}")
            return result

    result.setdefault("workflow_name", name)
    result.setdefault("requires_approval", workflow.requires_approval)
    return result
