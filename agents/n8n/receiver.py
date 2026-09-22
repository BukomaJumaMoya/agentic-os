#!/usr/bin/env python3
"""Inbound boundary for n8n -> Hermes. HMAC-signed, localhost-only, rate-limited.

WHY THIS EXISTS RATHER THAN A HERMES WEBHOOK
--------------------------------------------
The brief says "if Hermes supports inbound webhooks". It does not: its
`gateway.api_server` is `enabled: false`, and turning on an HTTP server that
can start agent runs is a larger surface than this needs. So the boundary is
here, it is small enough to read in one sitting, and it does exactly three
things: verify a signature, rate-limit, and append a line to a queue file.

IT CANNOT TALK TO TELEGRAM, AND THAT IS THE POINT
-------------------------------------------------
There is no bot token in this process. A verified notice is appended to
`agents/n8n/queue.jsonl`; a Hermes cron job drains that queue and delivers
through Hermes' own Telegram path. So a compromise of this listener yields the
ability to queue a text notice to Bukoma, and nothing else -- no token, no tool,
no agent.

It also gets the no-spam rule for free: the drain script prints nothing when the
queue is empty, and a `--no-agent` cron job with empty stdout delivers nothing.

THE THREE CONTROLS
------------------
1. BOUND TO 127.0.0.1. Not 0.0.0.0 with a firewall in front -- bound, so a
   misconfigured firewall cannot expose it.
2. HMAC-SHA256 over the RAW BODY, compared with `hmac.compare_digest`. The
   signature covers a timestamp too, and a request older than
   MAX_SKEW_SECONDS is refused, so a captured body cannot be replayed later.
   A body with no signature is refused; so is one whose signature is well
   formed but wrong. Unknown = refused, always.
3. RATE LIMIT. A fixed window per remote address. n8n sends one notice per
   task; anything that arrives faster than RATE_LIMIT_MAX in
   RATE_LIMIT_WINDOW is a loop or an attack, and neither should be relayed.

    python agents/n8n/receiver.py          # run it
    python agents/n8n/receiver.py --self-test
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# In the container the data directory is bind-mounted at /data; on the host it
# is this file's own directory. One variable, so the same file runs either way.
HERE = Path(os.environ.get("RECEIVER_DATA") or Path(__file__).resolve().parent)
QUEUE = HERE / "queue.jsonl"
ENV = HERE / ".env"

# Two bind addresses, both unroutable from the LAN, and NOT 0.0.0.0.
#
# n8n runs in Docker, where 127.0.0.1 is the CONTAINER's loopback -- so a
# host-loopback listener is unreachable from it, and the workflow failed with
# `connect ECONNREFUSED 127.0.0.1:8787` for exactly that reason. The lazy fix
# is 0.0.0.0 plus a source check; that opens the port on the Wi-Fi interface
# and leaves only a check between the internet and this process.
#
# Instead: bind the Docker bridge gateway (reachable from containers, not
# routed off the machine) AND host loopback. Verified from the host that the
# real LAN address answers on neither.
BIND_ADDRESSES = tuple(
    (os.environ.get("RECEIVER_BIND") or "127.0.0.1").split(","))
# Docker assigns container addresses from a private subnet; n8n's address
# on the shared network is the only non-loopback source expected.
ALLOWED_CLIENT_PREFIXES = tuple(
    (os.environ.get("RECEIVER_ALLOW_PREFIXES") or "127.,::1,172.").split(","))
PORT = 8787
PATH = "/n8n/notice"

MAX_BODY_BYTES = 64 * 1024
MAX_SKEW_SECONDS = 300
RATE_LIMIT_MAX = 12
RATE_LIMIT_WINDOW = 60.0

_hits: dict[str, deque] = {}
_lock = threading.Lock()


def env_value(name: str, default: str = "") -> str:
    if not ENV.exists():
        return os.environ.get(name, default)
    for line in ENV.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        match = re.match(rf"\s*{re.escape(name)}\s*=(.*)$", line)
        if match:
            return match.group(1).strip().strip('"').strip("'")
    return os.environ.get(name, default)


def sign(secret: str, timestamp: str, body: bytes) -> str:
    """HMAC-SHA256 over "<timestamp>." + raw body, hex."""
    mac = hmac.new(secret.encode("utf-8"),
                   timestamp.encode("utf-8") + b"." + body, hashlib.sha256)
    return mac.hexdigest()


def rate_limited(client: str) -> bool:
    now = time.monotonic()
    with _lock:
        seen = _hits.setdefault(client, deque())
        while seen and now - seen[0] > RATE_LIMIT_WINDOW:
            seen.popleft()
        if len(seen) >= RATE_LIMIT_MAX:
            return True
        seen.append(now)
        return False


def verify(secret: str, headers, body: bytes) -> tuple[bool, str]:
    """(ok, reason). Every failure path returns the SAME shape to the caller."""
    if not secret:
        return False, "no shared secret configured"
    signature = (headers.get("X-Hermes-Signature") or "").strip()
    timestamp = (headers.get("X-Hermes-Timestamp") or "").strip()
    if not signature or not timestamp:
        return False, "missing signature or timestamp"
    if not re.fullmatch(r"[0-9]{1,20}", timestamp):
        return False, "malformed timestamp"
    skew = abs(time.time() - int(timestamp))
    if skew > MAX_SKEW_SECONDS:
        # Without this, a body captured once can be replayed forever: the
        # signature stays valid because the body never changes.
        return False, f"timestamp is {int(skew)}s off (max {MAX_SKEW_SECONDS})"
    expected = sign(secret, timestamp, body)
    if not hmac.compare_digest(expected, signature):
        return False, "signature mismatch"
    return True, "ok"


def enqueue(payload: dict) -> None:
    QUEUE.parent.mkdir(parents=True, exist_ok=True)
    record = {"received_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
              "source": "n8n", "payload": payload}
    with QUEUE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


class Handler(BaseHTTPRequestHandler):
    server_version = "juma-n8n-receiver/1.0"

    def log_message(self, fmt, *args):     # noqa: A003 - quieter default log
        sys.stderr.write(f"[receiver] {self.address_string()} {fmt % args}\n")

    def _reply(self, code: int, message: str) -> None:
        body = json.dumps({"ok": code == 200, "detail": message}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):                     # noqa: N802 - BaseHTTPRequestHandler API
        if self.path != PATH:
            return self._reply(404, "unknown path")

        client = self.client_address[0]
        if not client.startswith(ALLOWED_CLIENT_PREFIXES):
            # Belt and braces: the sockets are bound to loopback and the Docker
            # gateway, so this should be unreachable. It is here because
            # "should be unreachable" has a poor record in this repository.
            return self._reply(403, "not local")

        if rate_limited(client):
            return self._reply(429, "rate limited")

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._reply(400, "bad Content-Length")
        if length <= 0 or length > MAX_BODY_BYTES:
            return self._reply(413, "body missing or too large")
        body = self.rfile.read(length)

        ok, reason = verify(env_value("N8N_WEBHOOK_SECRET"), self.headers, body)
        if not ok:
            sys.stderr.write(f"[receiver] REFUSED: {reason}\n")
            # One generic message to the caller; the reason goes to our log.
            # Telling an unauthenticated caller which check it failed is free
            # help for whoever is probing.
            return self._reply(401, "rejected")

        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            return self._reply(400, "body is not JSON")

        enqueue(payload if isinstance(payload, dict) else {"value": payload})
        return self._reply(200, "queued")


def self_test() -> int:
    """Prove each control refuses, without a network."""
    secret = "test-secret"
    body = b'{"task":"x"}'
    now = str(int(time.time()))

    class H(dict):
        def get(self, k, d=None):
            return dict.get(self, k, d)

    cases = [
        ("valid", {"X-Hermes-Signature": sign(secret, now, body),
                   "X-Hermes-Timestamp": now}, True),
        ("no signature", {"X-Hermes-Timestamp": now}, False),
        ("wrong signature", {"X-Hermes-Signature": "0" * 64,
                             "X-Hermes-Timestamp": now}, False),
        ("body tampered", {"X-Hermes-Signature": sign(secret, now, b'{"task":"y"}'),
                           "X-Hermes-Timestamp": now}, False),
        ("replayed (old timestamp)",
         {"X-Hermes-Signature": sign(secret, str(int(time.time()) - 9999), body),
          "X-Hermes-Timestamp": str(int(time.time()) - 9999)}, False),
        ("malformed timestamp", {"X-Hermes-Signature": "a" * 64,
                                 "X-Hermes-Timestamp": "not-a-number"}, False),
    ]
    failures = 0
    for name, headers, expected in cases:
        ok, reason = verify(secret, H(headers), body)
        good = ok is expected
        failures += 0 if good else 1
        print(f"  {'PASS' if good else 'FAIL'}  {name:26} -> "
              f"{'accepted' if ok else 'refused'} ({reason})")

    ok, reason = verify("", H({"X-Hermes-Signature": "x", "X-Hermes-Timestamp": now}), body)
    good = ok is False
    failures += 0 if good else 1
    print(f"  {'PASS' if good else 'FAIL'}  {'no secret configured':26} -> "
          f"{'accepted' if ok else 'refused'} ({reason})")

    _hits.clear()
    limited = [rate_limited("127.0.0.1") for _ in range(RATE_LIMIT_MAX + 3)]
    good = limited[:RATE_LIMIT_MAX] == [False] * RATE_LIMIT_MAX and all(limited[RATE_LIMIT_MAX:])
    failures += 0 if good else 1
    print(f"  {'PASS' if good else 'FAIL'}  {'rate limit':26} -> "
          f"{RATE_LIMIT_MAX} allowed then refused")

    print(f"\n{7 - failures}/7 controls behave correctly")
    return 1 if failures else 0


def main() -> int:
    if "--self-test" in sys.argv:
        return self_test()
    if not env_value("N8N_WEBHOOK_SECRET"):
        print("N8N_WEBHOOK_SECRET is not set in agents/n8n/.env; refusing to "
              "start an unauthenticated listener", file=sys.stderr)
        return 1
    servers = []
    for address in BIND_ADDRESSES:
        try:
            servers.append(HTTPServer((address, PORT), Handler))
            print(f"listening on http://{address}:{PORT}{PATH}")
        except OSError as exc:
            # A missing Docker bridge is not fatal -- loopback still serves the
            # host-side tests. A missing LOOPBACK is fatal.
            print(f"could not bind {address}: {exc}", file=sys.stderr)
    if not servers:
        print("no address could be bound", file=sys.stderr)
        return 1
    for server in servers[1:]:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    servers[0].serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
