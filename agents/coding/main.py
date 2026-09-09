#!/usr/bin/env python3
"""
Coding Agent — standalone bounded process.

Authority: READ | INTERNAL_WRITE
Purpose: bounded software-engineering responsibility
Forbidden: external business actions, unrelated repo changes
Verification: syntax check only
  py_compile for Python, `node --check` for JavaScript.
  Runs when a path is supplied; skipped otherwise.
"""

import sys
import json
import os
import re
import subprocess
from pathlib import Path

AGENT = "coding"
VERSION = "1.0.0"

BASE = Path(__file__).resolve().parent.parent.parent
WORKSPACE_ROOT = Path(os.getenv("CODING_AGENT_WORKSPACE") or (BASE / "workspace")).resolve()

# Windows reserved device names: writing to these hits a device, not a file.
_RESERVED_DEVICE_NAMES = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def resolve_write_target(output_dir, filename):
    """Resolve a write target inside WORKSPACE_ROOT, or raise ValueError.

    Rejects absolute paths, drive letters, UNC prefixes, '..' traversal and
    reserved Windows device names before resolving, then confirms the resolved
    path is still under the workspace root. ``Path(a) / b`` discards ``a``
    entirely when ``b`` is absolute, which is what made the original write
    primitive arbitrary.
    """
    name = filename or "generated_code.txt"
    for label, raw in (("output_dir", output_dir or "."), ("filename", name)):
        s = str(raw).strip()
        if not s:
            raise ValueError(f"{label} must not be empty")
        if s.startswith(("/", "\\")) or (len(s) > 1 and s[1] == ":"):
            raise ValueError(f"{label} must be relative to the workspace")
        parts = [seg for seg in re.split(r"[\\/]+", s) if seg not in ("", ".")]
        if any(seg == ".." for seg in parts):
            raise ValueError(f"{label} must not traverse with '..'")
        if any(seg.split(".")[0].lower() in _RESERVED_DEVICE_NAMES for seg in parts):
            raise ValueError(f"{label} must not use a reserved device name")
    target = (WORKSPACE_ROOT / (output_dir or ".") / name).resolve()
    if target != WORKSPACE_ROOT and WORKSPACE_ROOT not in target.parents:
        raise ValueError("resolved path escapes the workspace root")
    return target
ALLOWED_ACTIONS = {
    "generate",
    "review",
    "debug",
    "explain",
    "refactor",
    "tests",
    "document"
}
FORBIDDEN_ACTIONS = [
    "send_message",
    "external_action",
    "orchestrate",
    "approve",
    "publish",
    "deploy"
]


def syntax_check(path, language):
    path = Path(path)
    if not path.exists():
        return {"status": "skipped", "reason": "file not found"}
    try:
        if language in ("python", "py"):
            res = subprocess.run(
                [sys.executable, "-m", "py_compile", str(path)],
                capture_output=True, text=True, timeout=30
            )
            return {
                "status": "pass" if res.returncode == 0 else "fail",
                "stdout": res.stdout,
                "stderr": res.stderr
            }
        elif language in ("javascript", "js", "node"):
            res = subprocess.run(
                ["node", "--check", str(path)],
                capture_output=True, text=True, timeout=30
            )
            return {
                "status": "pass" if res.returncode == 0 else "fail",
                "stdout": res.stdout,
                "stderr": res.stderr
            }
    except Exception as e:
        return {"status": "error", "reason": str(e)}
    return {"status": "skipped", "reason": "unsupported language"}


def validate_input(data):
    if not isinstance(data, dict):
        return "Input must be a JSON object"
    action = data.get("action")
    if not action:
        return "Missing required field: action"
    if action not in ALLOWED_ACTIONS:
        return f"Unknown action: {action}. Allowed: {sorted(ALLOWED_ACTIONS)}"
    if action in ("generate", "refactor") and not data.get("prompt"):
        return f"Action '{action}' requires 'prompt'"
    if action in ("review", "debug", "explain", "tests", "document") and not data.get("code"):
        return f"Action '{action}' requires 'code'"
    return None


def main():
    try:
        data = json.load(sys.stdin)
    except Exception as e:
        print(json.dumps({"agent": AGENT, "error": f"Invalid input JSON: {e}"}))
        sys.exit(1)

    err = validate_input(data)
    if err:
        print(json.dumps({"agent": AGENT, "error": err}))
        sys.exit(1)

    action = data["action"]
    language = data.get("language", "python").lower()
    write_files = bool(data.get("write_files", False))
    output_dir = data.get("output_dir", ".")
    path = data.get("path")
    code = data.get("code", "")

    result = {
        "agent": AGENT,
        "version": VERSION,
        "action": action,
        "authority": "READ | INTERNAL_WRITE",
        "status": "success",
        "result": {},
        "verification": {},
        "security_warnings": []
    }

    if action == "generate":
        result["result"] = {"message": "generated content returned in result", "preview": code[:500]}
    else:
        result["result"] = {"message": f"{action} complete", "preview": code[:500]}

    verification = {}
    if path:
        verification["syntax"] = syntax_check(path, language)
    else:
        verification["syntax"] = {"status": "skipped", "reason": "no path provided"}
    result["verification"] = verification

    if write_files and code:
        try:
            target = resolve_write_target(output_dir, data.get("filename"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(code, encoding="utf-8")
            result["result"]["written_to"] = str(target)
        except ValueError as e:
            result["security_warnings"].append(f"Refused unsafe write target: {e}")
            result["status"] = "refused"
        except Exception as e:
            result["security_warnings"].append(f"File write failed: {e}")
            result["status"] = "partial"

    print(json.dumps(result, indent=2))
    sys.exit(0)


if __name__ == "__main__":
    main()
