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
import subprocess
from pathlib import Path

AGENT = "coding"
VERSION = "1.0.0"
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
            out = Path(output_dir)
            out.mkdir(parents=True, exist_ok=True)
            target = out / (data.get("filename") or "generated_code.txt")
            target.write_text(code, encoding="utf-8")
            result["result"]["written_to"] = str(target)
        except Exception as e:
            result["security_warnings"].append(f"File write failed: {e}")
            result["status"] = "partial"

    print(json.dumps(result, indent=2))
    sys.exit(0)


if __name__ == "__main__":
    main()
