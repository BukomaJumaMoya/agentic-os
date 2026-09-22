#!/usr/bin/env python3
"""An OpenAI-compatible logging proxy, for measuring what Hermes actually sends.

WHY THIS EXISTS
---------------
Every claim in the README about Hermes' token cost -- "the Telegram tool surface
measures ~6,866 tokens per model call", "a routing turn needs at least two
calls" -- came from watching real requests, not from reasoning about them. The
provider's own usage numbers are the only ones that count, because the tokeniser
is theirs.

Hermes is pointed at this proxy instead of the provider. It forwards the request
untouched, forwards the reply untouched, and writes one JSONL line per call with
what it saw on the way through.

    python tools/logging_proxy.py \
        --upstream https://generativelanguage.googleapis.com/v1beta/openai \
        --port 8799 --log evidence/gemini-routing.jsonl

Then in Hermes' config.yaml:

    providers:
      gemini:
        api: http://127.0.0.1:8799/v1
        key_env: GEMINI_API_KEY

CREDENTIALS
-----------
The proxy never holds a key. It forwards the client's Authorization header
verbatim and records only its length and last four characters -- enough to prove
Hermes sent the credential it was supposed to, not enough to be one. The log is
written to evidence/, which is gitignored for exactly this reason.
"""

from __future__ import annotations

import argparse
import json
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Headers worth keeping: anything a provider uses to describe a quota, plus the
# request id, which is what support asks for first.
INTERESTING = re.compile(
    r"(ratelimit|rate-limit|retry|quota|x-goog|x-request|x-served|request-id)", re.I)

STATE = {"upstream": "", "log": None, "n": 0, "sink": False,
         "bodies": None, "lock": threading.Lock()}

# Gemini's native surface authenticates with ``?key=…``, so a URL is a
# credential. Every URL is scrubbed before it is written or printed.
_SECRET_QUERY = re.compile(r"([?&](?:key|api_key|access_token)=)[^&\s]+", re.I)


def _redact(url: str) -> str:
    return _SECRET_QUERY.sub(r"\1[redacted]", str(url or ""))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _dump_body(entry: dict, raw: bytes) -> None:
    """Write the full request beside the log, when asked for.

    Counting tokens is not the same as knowing what was in them. When a run
    measures the wrong tool surface, the only thing that settles it is the
    request itself.
    """
    if not STATE["bodies"]:
        return
    path = STATE["bodies"] / f"req-{STATE['n'] + 1:04d}.json"
    try:
        path.write_bytes(raw)
        entry["body_dump"] = path.name
    except Exception as exc:
        entry["body_dump_error"] = str(exc)


def _record(entry: dict) -> None:
    with STATE["lock"]:
        STATE["n"] += 1
        entry["call"] = STATE["n"]
        line = json.dumps(entry, ensure_ascii=False)
        path = STATE["log"]
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    # A compact console line, so a run can be watched as it happens.
    print(f"[{entry['call']:>3}] {entry.get('status')} "
          f"{entry.get('latency_ms', 0):>6.0f}ms  "
          f"req={entry.get('request_chars', 0):>6}ch "
          f"tools={entry.get('tool_count', '?'):>2} "
          f"msgs={entry.get('message_count', '?'):>2}  "
          f"usage={entry.get('usage')}  "
          f"{entry.get('error') or ''}", flush=True)


def _describe_request(raw: bytes) -> dict:
    """Measure the request without changing it."""
    out = {"request_chars": len(raw)}
    try:
        body = json.loads(raw.decode("utf-8"))
    except Exception:
        return out
    out["model"] = body.get("model")
    msgs = body.get("messages") or []
    out["message_count"] = len(msgs)
    tools = body.get("tools") or []
    out["tool_count"] = len(tools)
    out["tool_names"] = [
        (t.get("function") or {}).get("name") for t in tools][:40]
    out["stream"] = bool(body.get("stream"))
    # Split the prompt the way the README does, so the numbers stay comparable.
    out["system_chars"] = sum(len(str(m.get("content") or ""))
                              for m in msgs if m.get("role") == "system")
    out["tools_chars"] = len(json.dumps(tools)) if tools else 0
    # Anything Hermes adds that a provider may reject -- reasoning_effort is the
    # one that has already cost a day of debugging on another provider.
    for k in ("reasoning_effort", "reasoning", "temperature", "tool_choice",
              "max_tokens", "max_completion_tokens", "service_tier"):
        if k in body:
            out[f"param.{k}"] = body[k]
    return out


def _usage_from(payload, is_sse):
    try:
        if not is_sse:
            return (json.loads(payload.decode("utf-8")) or {}).get("usage")
        # Streaming: usage rides on one of the last data: frames.
        usage = None
        for line in payload.decode("utf-8", "replace").splitlines():
            if not line.startswith("data:"):
                continue
            chunk = line[5:].strip()
            if not chunk or chunk == "[DONE]":
                continue
            try:
                obj = json.loads(chunk)
            except Exception:
                continue
            if obj.get("usage"):
                usage = obj["usage"]
        return usage
    except Exception:
        return None


def _tool_calls_from(payload, is_sse):
    """How many tool calls came back -- the thing a routing turn is made of."""
    try:
        if is_sse:
            frames = set()
            for line in payload.decode("utf-8", "replace").splitlines():
                if line.startswith("data:") and '"tool_calls"' in line:
                    frames.add(line)
            return len(frames)
        obj = json.loads(payload.decode("utf-8"))
        msg = ((obj.get("choices") or [{}])[0]).get("message") or {}
        return len(msg.get("tool_calls") or [])
    except Exception:
        return None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # the JSONL is the log
        pass

    def do_POST(self):
        self._proxy("POST")

    def do_GET(self):
        self._proxy("GET")

    def _proxy(self, method):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""

        # /v1/chat/completions -> <upstream>/chat/completions
        path = self.path
        suffix = path[3:] if path.startswith("/v1/") else path
        url = STATE["upstream"].rstrip("/") + "/" + suffix.lstrip("/")

        # Gemini's native surface takes the key as ``?key=…`` rather than a
        # header, so the URL is a credential. Never let it reach the log.
        entry = {"ts": _now(), "method": method,
                 "path": _redact(path), "upstream": _redact(url)}
        entry.update(_describe_request(raw))

        auth = self.headers.get("Authorization") or ""
        entry["auth_sent"] = bool(auth)
        entry["auth_len"] = len(auth)
        entry["auth_tail"] = auth[-4:] if len(auth) > 8 else ""

        # Sink mode answers from here instead of forwarding, so the tool
        # surface can be inspected without spending a request against a quota.
        # The surface has to be right BEFORE the real turns are spent -- a run
        # measured on the wrong toolset is worse than no run, because it looks
        # like an answer.
        if STATE["sink"] and suffix.lstrip("/").startswith("chat/completions"):
            entry["latency_ms"] = 0.0
            entry["status"] = 200
            entry["sink"] = True
            _dump_body(entry, raw)
            _record(entry)
            model = entry.get("model") or "sink"
            text = "sink: no upstream call"
            if entry.get("stream"):
                # Hermes streams by default and rejects a non-SSE body with
                # "empty stream with no finish_reason", which looks like a
                # provider fault rather than a sink that answered the wrong
                # shape. Emit real SSE so a sink run fails only for real reasons.
                base = {"id": "sink", "object": "chat.completion.chunk",
                        "created": int(time.time()), "model": model}
                frames = [
                    {**base, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]},
                    {**base, "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]},
                    {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                     "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}},
                ]
                body = "".join(f"data: {json.dumps(f)}\n\n" for f in frames)
                body += "data: [DONE]\n\n"
                canned = body.encode()
                ctype = "text/event-stream"
            else:
                canned = json.dumps({
                    "id": "sink", "object": "chat.completion",
                    "created": int(time.time()), "model": model,
                    "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant", "content": text}}],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0,
                              "total_tokens": 0},
                }).encode()
                ctype = "application/json"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(canned)))
            self.end_headers()
            self.wfile.write(canned)
            return

        # Sink with no upstream: Hermes probes half a dozen capability paths
        # (/api/tags, /v1/models, /api/show) before the first turn. Answer them
        # 404 rather than trying to forward to an empty base URL.
        if STATE["sink"] and not STATE["upstream"]:
            entry["latency_ms"] = 0.0
            entry["status"] = 404
            entry["sink"] = True
            _record(entry)
            body = b'{"error":{"message":"sink: not a chat/completions path"}}'
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        req = urllib.request.Request(url, data=raw or None, method=method)
        for k, v in self.headers.items():
            if k.lower() in ("host", "content-length", "connection",
                             "accept-encoding"):
                continue
            req.add_header(k, v)
        req.add_header("Accept-Encoding", "identity")

        t0 = time.time()
        try:
            ctx = ssl.create_default_context()
            with urllib.request.urlopen(req, timeout=300, context=ctx) as r:
                status, hdrs, payload = r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            status, hdrs, payload = e.code, dict(e.headers), e.read()
        except Exception as e:
            entry["latency_ms"] = (time.time() - t0) * 1000
            entry["status"] = 0
            entry["error"] = f"{type(e).__name__}: {e}"
            _record(entry)
            body = json.dumps({"error": {"message": entry["error"]}}).encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        entry["latency_ms"] = (time.time() - t0) * 1000
        entry["status"] = status
        entry["response_chars"] = len(payload)
        _dump_body(entry, raw)
        is_sse = "text/event-stream" in (hdrs.get("Content-Type") or "")
        entry["sse"] = is_sse
        entry["usage"] = _usage_from(payload, is_sse)
        entry["tool_calls_returned"] = _tool_calls_from(payload, is_sse)
        entry["rate_limit_headers"] = {
            k: v for k, v in hdrs.items() if INTERESTING.search(k)}
        entry["all_response_headers"] = sorted(hdrs.keys())
        if status >= 400:
            entry["error_body"] = payload.decode("utf-8", "replace")[:1200]
            entry["error"] = f"HTTP {status}"
        _record(entry)

        self.send_response(status)
        for k, v in hdrs.items():
            if k.lower() in ("transfer-encoding", "content-length",
                             "connection", "content-encoding"):
                continue
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--upstream", required=not any(
        a == "--sink" for a in sys.argv), default="")
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--log", required=True)
    ap.add_argument("--sink", action="store_true",
                    help="answer chat/completions locally; never call upstream")
    ap.add_argument("--dump-bodies", metavar="DIR",
                    help="write each full request body into DIR")
    args = ap.parse_args()

    log = Path(args.log)
    log.parent.mkdir(parents=True, exist_ok=True)
    STATE["upstream"] = args.upstream
    STATE["log"] = log
    STATE["sink"] = bool(args.sink)
    if args.dump_bodies:
        bodies = Path(args.dump_bodies)
        bodies.mkdir(parents=True, exist_ok=True)
        STATE["bodies"] = bodies

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    mode = "SINK (no upstream calls)" if args.sink else args.upstream
    print(f"proxy: 127.0.0.1:{args.port} -> {mode}", flush=True)
    print(f"log:   {log}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
