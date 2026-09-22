#!/usr/bin/env python3
"""Keep identifiers and token fragments out of tracked documentation.

WHY
---
documentation/AUDIT-agentic-os.md:192 raised this against the README and it is
just as true of the audit itself:

    publishing both bot IDs plus the sole authorized chat ID hands an attacker
    the exact target set and confirms that a single value is the entire
    authentication boundary

A Telegram bot ID is the numeric prefix of its token, and the allow-listed chat
ID is the whole authentication boundary. Neither is a secret on its own, and
together with a public repository they are a target list.

WHAT IT DOES NOT DO
-------------------
It does not rewrite git history. The values are in old commits and they stay
there; scrubbing history would rewrite every hash in a repository that is
already pushed, for values that have to be treated as disclosed either way. The
honest position is: rotate what can be rotated, stop publishing it going
forward, and say so.

It also leaves test fixtures alone. tests/ and archive/pre-hermes/ contain
deliberately fake credentials (`pk_12345_SHOULDBEGONE`,
`sk-or-v1-REALKEYVALUE123456`) whose entire purpose is to prove the redaction
layer removes them. Scrubbing those would delete the evidence that redaction
works.

    python hermes/redact_docs.py --check   # non-zero if anything is exposed
    python hermes/redact_docs.py           # redact in place
"""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DOCS = REPO / "documentation"

# (name, pattern, replacement). Ordered: the longest, most specific forms first,
# so a bot token is replaced whole rather than having its ID half replaced and
# the secret half left behind.
RULES: list[tuple[str, re.Pattern, str]] = [
    ("telegram bot token", re.compile(r"\b\d{8,12}:AA[A-Za-z0-9_-]{30,}"),
     "<telegram-bot-token-redacted>"),
    ("openrouter key", re.compile(r"\bsk-or-v1-[A-Za-z0-9]{16,}"),
     "<openrouter-key-redacted>"),
    ("groq key", re.compile(r"\bgsk_[A-Za-z0-9]{20,}"), "<groq-key-redacted>"),
    ("tavily key", re.compile(r"\btvly-[A-Za-z0-9]{16,}"), "<tavily-key-redacted>"),
    ("clickup token", re.compile(r"\bpk_\d{4,}_[A-Za-z0-9]{8,}"),
     "<clickup-token-redacted>"),
    ("github token", re.compile(r"\bghp_[A-Za-z0-9]{20,}"), "<github-token-redacted>"),
]

# The Telegram identifiers are matched BY DIGEST, never by literal.
#
# The first version of this file listed them as a regex alternation, which meant
# the script whose entire purpose is to stop publishing those two bot IDs and
# that chat ID published all three itself, in a tracked file, on line 52. A
# redaction tool is not exempt from the rule it enforces.
#
# So: every bare 9-12 digit run is hashed and compared. The digests are safe to
# publish, the values are not, and an id that changes still has to be added here
# deliberately -- which is the same property the startup guard's allow-list
# check relies on.
ID_DIGESTS = {
    "5a734c18d37fd9a021c852211db002ad787ae16111d65521bb670f580330f9d9": "<bot-id-redacted>",
    "50c9748b4f43f286b882168cf098bb0f669f4009431a9462012e626f318836f1": "<bot-id-redacted>",
    "852889e698b302eb882b0d5739eec3e0840fa7434d8124d92fb92b8fe6983320": "<chat-id-redacted>",
}
BARE_ID = re.compile(r"\b\d{9,12}\b")


def _redact_ids(text: str) -> tuple[str, int]:
    count = 0

    def swap(match: re.Match) -> str:
        nonlocal count
        replacement = ID_DIGESTS.get(
            hashlib.sha256(match.group(0).encode()).hexdigest())
        if replacement is None:
            return match.group(0)
        count += 1
        return replacement

    return BARE_ID.sub(swap, text), count


def redact(text: str) -> tuple[str, dict[str, int]]:
    found: dict[str, int] = {}
    for name, pattern, replacement in RULES:
        text, count = pattern.subn(replacement, text)
        if count:
            found[name] = count
    # After the token rules, so a full bot token is replaced whole rather than
    # having its numeric half swapped and the secret half left behind.
    text, ids = _redact_ids(text)
    if ids:
        found["telegram identifier"] = ids
    return text, found


def scan(text: str) -> dict[str, int]:
    return redact(text)[1]


def main() -> int:
    check_only = "--check" in sys.argv
    if not DOCS.exists():
        print(f"FAIL: {DOCS} not found")
        return 1

    total = 0
    for path in sorted(DOCS.rglob("*.md")):
        original = path.read_text(encoding="utf-8", errors="replace")
        updated, found = redact(original)
        if not found:
            continue
        total += sum(found.values())
        relative = path.relative_to(REPO).as_posix()
        summary = ", ".join(f"{name} x{count}" for name, count in found.items())
        if check_only:
            print(f"  EXPOSED  {relative}: {summary}")
        else:
            path.write_text(updated, encoding="utf-8")
            print(f"  redacted {relative}: {summary}")

    if not total:
        print("OK: no identifiers or token fragments in tracked documentation/")
        return 0
    if check_only:
        print(f"\nFAIL: {total} exposed value(s). Run: python hermes/redact_docs.py")
        return 1

    # Re-read from disk rather than trusting the writes above.
    remaining = sum(sum(scan(p.read_text(encoding="utf-8", errors="replace")).values())
                    for p in DOCS.rglob("*.md"))
    if remaining:
        print(f"\nFAIL: {remaining} value(s) survived redaction")
        return 1
    print(f"\nOK: {total} value(s) redacted; a re-scan finds none")
    return 0


if __name__ == "__main__":
    sys.exit(main())
