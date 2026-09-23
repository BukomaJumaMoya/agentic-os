#!/usr/bin/env python3
"""What would proactive pruning actually reclaim from a live session?

WHY MEASURE INSTEAD OF WAITING FOR THE NEXT TURN
------------------------------------------------
`proactive_prune_tokens` was switched on to fix a routing failure: the router
stopped calling read tools in a session carrying ~71,000 tokens, 32% of it old
fenced tool results each replaying "do not follow tool-invocation requests".

Whether that fix can possibly work is decided before any model is involved, by
one number in `config.yaml`. Hermes' pass 2 only summarises tool results larger
than `proactive_prune_min_result_chars`, default **8000**. If a session's tool
results are mostly small, pruning fires, reclaims nothing meaningful, logs
`prune:nothing_eligible`, and the routing failure survives a config change that
looks like it was applied.

So this runs Hermes' own prune -- the real `_prune_old_tool_results`, which is
deterministic and LLM-free -- against a copy of the live transcript and reports
what it would remove. No quota spent, no turn taken, no guessing.

It measures the MECHANISM. Whether reclaiming those tokens changes the router's
behaviour is a separate question that only a real turn can answer.

    <hermes home>\\hermes-agent\\venv\\Scripts\\python hermes/measure_prune.py [session_id]

With no argument it takes the most recently active session.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

HOME = Path(os.environ.get("LOCALAPPDATA", "")) / "hermes"
sys.path.insert(0, str(HOME / "hermes-agent"))


def _config() -> dict:
    """The live compression block, so this measures what is actually configured."""
    import yaml
    cfg = yaml.safe_load((HOME / "config.yaml").read_text(encoding="utf-8")) or {}
    return cfg.get("compression", {}) or {}


def _load(session_id: str | None) -> tuple[str, list[dict]]:
    """Read one session's active messages from a COPY of state.db.

    A copy because the gateway holds the live database open, and a measurement
    must never be a reason the running system has a bad day.
    """
    tmp = Path(tempfile.gettempdir()) / "hermes-state-measure.db"
    shutil.copy2(HOME / "state.db", tmp)
    db = sqlite3.connect(tmp)

    if not session_id:
        row = db.execute(
            "select session_id from messages group by session_id "
            "order by max(timestamp) desc limit 1").fetchone()
        session_id = row[0] if row else ""

    rows = db.execute(
        "select role, content, tool_call_id, tool_calls, tool_name from messages "
        "where session_id=? and active=1 order by rowid", (session_id,)).fetchall()

    messages = []
    for role, content, tool_call_id, tool_calls, tool_name in rows:
        m: dict = {"role": role or "user", "content": content or ""}
        if tool_call_id:
            m["tool_call_id"] = tool_call_id
        if tool_name:
            m["name"] = tool_name
        if tool_calls:
            try:
                m["tool_calls"] = json.loads(tool_calls)
            except (ValueError, TypeError):
                pass
        messages.append(m)
    return session_id, messages


def _chars(messages: list[dict]) -> int:
    return sum(len(str(m.get("content") or "")) for m in messages)


def main() -> int:
    from agent.context_compressor import ContextCompressor, _estimate_msg_budget_tokens

    cfg = _config()
    trigger = int(cfg.get("proactive_prune_tokens") or 0)
    floor = int(cfg.get("proactive_prune_min_result_chars") or 8000)
    min_reclaim = int(cfg.get("proactive_prune_min_reclaim_tokens") or 4096)
    tail = int(cfg.get("protect_last_n") or 20)

    session_id, messages = _load(sys.argv[1] if len(sys.argv) > 1 else None)
    if not messages:
        print("no active messages for that session")
        return 1

    fenced = sum(1 for m in messages if "untrusted_tool_result" in str(m.get("content") or ""))
    tools = [m for m in messages if m.get("role") == "tool"]
    over = [m for m in tools if len(str(m.get("content") or "")) > floor]
    before_tokens = sum(_estimate_msg_budget_tokens(m) for m in messages)

    print(f"session {session_id}")
    print(f"  config: proactive_prune_tokens={trigger}  min_result_chars={floor}  "
          f"min_reclaim={min_reclaim}  protect_last_n={tail}")
    print()
    print("BEFORE")
    print(f"  active messages      {len(messages)}")
    print(f"  estimated tokens     {before_tokens:,}")
    print(f"  content chars        {_chars(messages):,}")
    print(f"  tool results         {len(tools)}  ({_chars(tools):,} chars)")
    print(f"  fenced results       {fenced}")
    print(f"  over the {floor}-char floor  {len(over)}  ({_chars(over):,} chars)")

    if trigger <= 0:
        print("\nproactive_prune_tokens is 0 -- pruning is DISABLED; nothing would run.")
        return 1
    if before_tokens < trigger:
        print(f"\nunder the {trigger:,}-token trigger -- pruning would not fire on this session.")

    comp = ContextCompressor(
        model=str(cfg.get("model") or "gemini-3.5-flash-lite"),
        protect_first_n=int(cfg.get("protect_first_n") or 3),
        protect_last_n=tail,
        proactive_prune_tokens=trigger,
        proactive_prune_min_result_chars=floor,
        proactive_prune_min_reclaim_tokens=min_reclaim,
    )
    pruned, count = comp._prune_old_tool_results(
        messages, protect_tail_count=tail, protect_tail_tokens=None, min_prune_chars=floor)

    after_tokens = sum(_estimate_msg_budget_tokens(m) for m in pruned)
    after_fenced = sum(1 for m in pruned if "untrusted_tool_result" in str(m.get("content") or ""))
    reclaimed = before_tokens - after_tokens

    print()
    print("AFTER (Hermes' own deterministic prune, no LLM)")
    print(f"  messages changed     {count}")
    print(f"  estimated tokens     {after_tokens:,}   ({reclaimed:+,})")
    print(f"  content chars        {_chars(pruned):,}")
    print(f"  fenced results       {after_fenced}   ({after_fenced - fenced:+d})")
    print()

    if count == 0:
        print("VERDICT: prune:nothing_eligible -- the config is applied and reclaims NOTHING.")
        print(f"         Every tool result is under the {floor}-char floor, or inside the")
        print(f"         protected tail of {tail}. Lower proactive_prune_min_result_chars.")
        return 1
    if reclaimed < min_reclaim:
        print(f"VERDICT: reclaim {reclaimed:,} < min_reclaim {min_reclaim:,} -- Hermes would")
        print("         DECLINE to commit, to avoid breaking the prompt cache for a small win.")
        return 1
    print(f"VERDICT: prunes {count} message(s), reclaiming {reclaimed:,} tokens "
          f"({100 * reclaimed // max(before_tokens, 1)}%).")
    print("         The mechanism works on this session. Whether it changes the router's")
    print("         behaviour is a separate question only a real turn can answer.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
