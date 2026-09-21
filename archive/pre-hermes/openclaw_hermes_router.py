#!/usr/bin/env python3
"""
HTTP adapter for OpenClaw → Hermes router.

This adapter exposes router behavior in testable form without importing Node
files from Python. Integration tests should call this via subprocess or use
the Node test suite.
"""

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
ROUTER_SCRIPT = BASE / "tools" / "openclaw-hermes-router.js"


def _invoke_local(message: str, timeout: int = 120) -> dict:
    if not ROUTER_SCRIPT.exists():
        return {"ok": False, "reply": "", "stderr": f"missing script: {ROUTER_SCRIPT}", "returnCode": -1}
    try:
        proc = subprocess.run(
            ["node", str(ROUTER_SCRIPT)],
            input=json.dumps({"message": message}),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()
        if proc.returncode == 0 and stdout:
            try:
                parsed = json.loads(stdout)
                return parsed
            except Exception:
                return {"ok": True, "reply": stdout, "stderr": stderr, "returnCode": proc.returncode}
        return {"ok": False, "reply": "", "stderr": stderr or f"Empty output from router (exit {proc.returncode})", "returnCode": proc.returncode}
    except subprocess.TimeoutExpired:
        return {"ok": False, "reply": "", "stderr": "router invocation timed out", "returnCode": -1}
    except Exception as e:
        return {"ok": False, "reply": "", "stderr": str(e), "returnCode": -1}


def _post(path: str, payload: dict, timeout: int = 30) -> dict:
    router_host = os.getenv("OPENCLAW_HERMES_ROUTER_HOST", "127.0.0.1")
    router_port = int(os.getenv("OPENCLAW_HERMES_ROUTER_PORT", "18790"))
    url = f"http://{router_host}:{router_port}{path}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            try:
                return json.loads(resp.read().decode("utf-8"))
            except Exception:
                return {"ok": True, "status": int(resp.status)}
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8")
        except Exception:
            body = ""
        return {"ok": False, "status": int(e.code), "error": body}
    except Exception as e:
        return {"ok": False, "status": 0, "error": str(e)}


def invoke_hermes(message: str, retries: int = 2) -> dict:
    last = None
    for _ in range(max(1, retries)):
        last = _invoke_local(message)
        if last.get("ok"):
            return last
    return last or {"ok": False, "reply": "", "stderr": "router unavailable", "returnCode": -1}


def send_telegram_message(text: str, to: str = None) -> dict:
    return _post("/invoke", {"message": text, "query": text})


def main():
    try:
        data = json.load(sys.stdin)
    except Exception as e:
        print(json.dumps({"error": f"Invalid input JSON: {e}"}))
        sys.exit(1)

    message = data.get("message") or data.get("query") or ""
    if not message:
        print(json.dumps({"error": "Missing message"}))
        sys.exit(1)

    result = invoke_hermes(message, retries=int(data.get("retries", 2)))
    print(json.dumps(result, indent=2))
    sys.exit(0 if result.get("ok") else 2)


if __name__ == "__main__":
    main()
