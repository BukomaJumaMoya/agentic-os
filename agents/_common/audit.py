#!/usr/bin/env python3
"""Per-agent append-only audit log.

One JSONL file per agent per day under logs/<agent>/. Gitignored, because it
records real client questions, real task names and real API responses.

WHAT IT IS FOR
--------------
An agent that Hermes calls over stdio is invisible: there is no terminal, no
console, and a tool that returns `{"ok": false}` says nothing about which of
five upstream calls failed. The audit log is the only place the full story
exists. It is also the answer to "what did the system actually do on Tuesday",
which matters more once agents start creating tasks in a real ClickUp space.

Every record goes through the same redactor the tool returns use, so the log is
safe to read and safe to paste into an issue. That is deliberate: a log people
are afraid to share is a log nobody reads.

Writes never raise. A full disk or a locked file must degrade the logging, not
fail the client's research question.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import errors

REPO_ROOT = Path(__file__).resolve().parents[2]


class Audit:
    def __init__(self, agent: str, log_dir: Path | None = None):
        self.agent = agent
        self.dir = Path(log_dir) if log_dir else REPO_ROOT / "logs" / agent
        self._lock = threading.Lock()
        self._broken = False
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            self._broken = True

    @property
    def path(self) -> Path:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return self.dir / f"{self.agent}-{day}.jsonl"

    def write(self, event: str, **fields: Any) -> None:
        if self._broken:
            return
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "agent": self.agent,
            "pid": os.getpid(),
            "event": event,
        }
        for key, value in fields.items():
            record[key] = _clean(value)
        try:
            line = json.dumps(record, ensure_ascii=False, default=str)
        except Exception:
            line = json.dumps({**{k: str(v) for k, v in record.items()},
                               "_note": "fields were not JSON-serialisable"})
        line = errors.redact(line)
        try:
            with self._lock:
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
        except Exception:
            # Logging must never be the reason a tool call fails.
            self._broken = True


def _clean(value: Any, *, limit: int = 4000) -> Any:
    """Cap anything long. A 400 KB page body in a log line helps nobody."""
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + f"... [{len(value) - limit} more chars]"
    if isinstance(value, dict):
        return {k: _clean(v, limit=limit) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean(v, limit=limit) for v in value[:50]]
    return value
