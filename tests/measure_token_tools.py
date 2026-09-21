#!/usr/bin/env python3
"""A/B the coding agent's token-reduction tools on one identical task.

Run: agents/.venv/Scripts/python tests/measure_token_tools.py

Runs the same instruction twice through the real MCP server -- once with RTK
and Ponytail disabled, once with them enabled -- and reports input/output
tokens, the size of the observed git diff, and whether the task actually
succeeded.

Two things this deliberately does NOT do:

  - average over several runs. One sample per arm is not a benchmark, and the
    output says so rather than implying a precision it does not have.
  - retry on a rate limit. Groq's free tier is 8,000 tokens/minute; if an arm
    trips it, that is the result for that arm and it is reported as such,
    because retrying would silently change what is being measured.

Success is judged from the observed diff and the project's own files, never
from the coding model's summary of what it did.
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests"))

from mcp_client import MCPStdioClient  # noqa: E402

PY = str(REPO / "agents" / ".venv" / "Scripts" / "python.exe")
AGENT = str(REPO / "agents" / "coding" / "main.py")
DEV_ROOT = Path(r"D:\Bukoma Juma Moya\Dev")

INSTRUCTION = (
    "Create fizzbuzz.py with a function fizzbuzz(n) returning the FizzBuzz "
    "string for n ('Fizz' for multiples of 3, 'Buzz' for 5, 'FizzBuzz' for "
    "both, otherwise the number as a string). Then run it to print fizzbuzz "
    "for 1 to 15."
)


def run_arm(label: str, token_tools: bool) -> dict:
    project = DEV_ROOT / "scratch" / f"ab-{'on' if token_tools else 'off'}"
    shutil.rmtree(project, ignore_errors=True)

    started = time.time()
    with MCPStdioClient([PY, AGENT], timeout=1200) as client:
        begun = client.call("start_code_task", {
            "project_path": str(project),
            "instruction": INSTRUCTION,
            "kind": "backend",
            "token_tools": token_tools,
        })
        if not begun.get("ok"):
            return {"arm": label, "started": False, "error": begun}

        job_id = begun["job_id"]
        for _ in range(240):
            status = client.call("get_status", {"job_id": job_id})
            if status.get("finished"):
                break
            time.sleep(5)
        result = client.call("get_result", {"job_id": job_id})

    elapsed = round(time.time() - started, 1)

    if not result.get("ok"):
        return {"arm": label, "started": True, "ok": False,
                "seconds": elapsed, "error": result}

    payload = result["result"]
    created = project / "fizzbuzz.py"
    return {
        "arm": label,
        "started": True,
        "ok": True,
        "seconds": elapsed,
        "sandbox": payload.get("sandbox"),
        "token_tools": payload.get("token_tools"),
        "input_tokens": payload.get("input_tokens"),
        "output_tokens": payload.get("output_tokens"),
        "tool_calls": payload.get("tool_calls"),
        "model_calls": payload.get("model_calls"),
        "cache_read_tokens": payload.get("cache_read_tokens"),
        "insertions": payload.get("insertions"),
        "lines_added_total": payload.get("lines_added_total"),
        "deletions": payload.get("deletions"),
        "changed_files": [f["path"] for f in payload.get("changed_files") or []],
        # Judged from disk, not from the model's account of itself.
        "fizzbuzz_exists": created.exists(),
        "fizzbuzz_lines": (len(created.read_text(encoding="utf-8").splitlines())
                           if created.exists() else 0),
        "agent_report": (payload.get("agent_report") or "")[:400],
    }


def main() -> int:
    results = []
    for label, tools in (("tools OFF (baseline)", False), ("tools ON (RTK+Ponytail)", True)):
        print(f"\n=== running: {label} ===", flush=True)
        outcome = run_arm(label, tools)
        results.append(outcome)
        print(json.dumps(outcome, indent=2)[:1200], flush=True)

    print("\n" + "=" * 72)
    print(f"{'arm':<26} {'in':>8} {'out':>8} {'calls':>6} {'+lines':>7} {'sec':>6}  passed")
    print("-" * 72)
    for r in results:
        if not r.get("ok"):
            detail = str(r.get("error", {}).get("error", "failed"))[:28]
            print(f"{r['arm']:<26} {'-':>8} {'-':>8} {'-':>6} {'-':>7} "
                  f"{r.get('seconds', 0):>6}  FAILED: {detail}")
            continue
        # An arm that spent zero tokens did not run, it was throttled. Calling
        # that a tool failure is how RTK and Ponytail were first (wrongly)
        # blamed for breaking the agent: whichever arm ran SECOND inside one
        # Groq 8,000-token minute came back empty, and the empty result looked
        # exactly like the tools doing nothing.
        if not r.get("input_tokens"):
            passed = "INCONCLUSIVE (no tokens spent - rate limited, re-run alone)"
        else:
            passed = "yes" if r["fizzbuzz_exists"] else "no"
        print(f"{r['arm']:<26} {str(r['input_tokens']):>8} {str(r['output_tokens']):>8} "
              f"{str(r['tool_calls']):>6} {str(r['lines_added_total']):>7} "
              f"{r['seconds']:>6}  {passed}")
    print("=" * 72)
    print("One run per arm. Indicative, not a benchmark.")
    print("Groq free tier is 8,000 tokens/minute; the arms are spaced so they")
    print("do not measure each other. An INCONCLUSIVE arm means re-run it alone.")

    Path(REPO / "evidence").mkdir(exist_ok=True)
    out = REPO / "evidence" / "token-tools-ab.json"
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"raw results: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
