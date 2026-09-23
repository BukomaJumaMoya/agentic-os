#!/usr/bin/env python3
"""Find credential copies nobody is managing, and say which are already dead.

WHY A DEAD KEY IS WORSE THAN NO KEY
-----------------------------------
`check_keys.py` answers "is every credential I know about still valid?".
This answers the opposite and more dangerous question: **what credentials exist
on this disk that nothing is tracking?**

Rotation only helps for copies you know about. Every tool that edits a config
takes a backup first, and those backups are not covered by the approval patch --
which matches `config.yaml`, `.env`, `auth.json` and `mcp-tokens`, not
`backups/config/config.yaml.bak.20260908_144345`. Rotating fixes the live file
and leaves the copies.

Measured on 2026-09-23, after the config-backup pile had already been pruned to
three: **nine more files** in `%LOCALAPPDATA%\\hermes\\backups` and
`.curator_backups` held live-SHAPED credentials, every one of them a rotated-out
value -- five 2026-09-08 configs, a "known good" config, a full 27 KB pre-rebuild
`.env`, its config.yaml, and a curator blob with four ClickUp tokens.

None of them worked. That is the point: a plausible dead key found later is
worse than none. It gets tried, it gets pasted into a form, it gets filed as
"the credential" in somebody's notes, and it costs an afternoon before anyone
checks whether it was ever live.

HOW IT DECIDES
--------------
Shapes, then digests -- never a value, not even a prefix:

  1. match known credential SHAPES (a Telegram token, `AIza…`, `gsk_…`, …)
  2. SHA-256 each match and compare the first 10 hex against the digests of the
     credentials currently in use
  3. a match that is NOT in use is STALE. A match that IS in use is an
     unmanaged live copy -- reported louder, because rotating will miss it

`--delete-stale` removes only files whose every match is stale. A file holding
anything still live is never deleted automatically; it is named, and what to do
about it is a decision, not a sweep.

    agents/.venv/Scripts/python hermes/scan_stale_credentials.py
    agents/.venv/Scripts/python hermes/scan_stale_credentials.py --delete-stale
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOME = Path(os.environ.get("LOCALAPPDATA", "")) / "hermes"

# Shape only. Deliberately loose on length: a truncated paste is still a leak.
PATTERNS = {
    "telegram bot token": re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}"),
    # Two shapes, because Google issues both: the classic `AIza…` and the newer
    # `AQ.A…` AI Studio key. The second is why this scanner first reported a
    # clean disk while a 53-character Gemini key sat in the very file it was
    # reading. A false negative from a pattern list is the one way a tool like
    # this actively makes things worse, so the managed files are used as a
    # self-check: every credential in them must match some pattern here.
    "gemini key":         re.compile(r"\bAIza[A-Za-z0-9_-]{30,}"),
    "gemini studio key":  re.compile(r"\bAQ\.A[A-Za-z0-9._-]{30,}"),
    # `github_pat_…` is the fine-grained PAT; it does NOT match `gh[pousr]_`.
    "github fine-grained": re.compile(r"\bgithub_pat_[A-Za-z0-9_]{50,}"),
    "groq key":           re.compile(r"\bgsk_[A-Za-z0-9]{40,}"),
    "openrouter key":     re.compile(r"\bsk-or-v1-[a-f0-9]{48,}"),
    "tavily key":         re.compile(r"\btvly-[A-Za-z0-9_-]{20,}"),
    "github token":       re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    "clickup token":      re.compile(r"\bpk_\d+_[A-Za-z0-9]{20,}"),
    "kola key":           re.compile(r"\bkola_live_[A-Za-z0-9]{16,}"),
    # n8n's API key is a JWT. Three base64url segments is specific enough to
    # be safe; a bare hex or base64 run is not, and that matters more here than
    # it looks -- see the note on --delete-stale below.
    "jwt (n8n api key)":  re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
}

# Some secrets have no distinctive shape at all: `N8N_WEBHOOK_SECRET` is 64 hex
# characters, indistinguishable from a SHA-256 digest, and this repository's own
# documentation is full of those.
#
# A loose pattern would be worse than no pattern. Every hex run it matched would
# digest to something not in the live set, be classified STALE, and be offered
# to `--delete-stale` -- a tool that deletes files because they contain a
# checksum. So shapeless secrets are found by searching for their EXACT current
# value instead, read from the managed `.env` files and never printed.
#
# The limit is honest and worth stating: this finds unmanaged copies of the
# secret in use, and cannot find a rotated-out one. Nothing can, without knowing
# the old value.
SHAPELESS = ("N8N_WEBHOOK_SECRET",)

# Where credentials are SUPPOSED to live. These are never swept.
IN_USE = [HOME / ".env", *sorted(REPO.glob("agents/*/.env"))]

# Everything else that has been observed to accumulate copies.
SWEEP = [
    (HOME, "config.yaml.bak-*"),
    (HOME, "*.bak"),
    (HOME / "backups", "**/*"),
    (HOME / ".curator_backups", "**/*"),
    (REPO / "archive", "**/*"),
    (REPO / "agents", "**/.env.bak*"),
    (REPO / "documentation", "**/*.md"),
]

SKIP_PARTS = {"node_modules", ".git", "__pycache__"}


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:10]


def _matches(text: str) -> dict[str, set[str]]:
    """{kind: {digest, ...}} for every credential-shaped string in *text*."""
    out: dict[str, set[str]] = {}
    for kind, rx in PATTERNS.items():
        found = {_digest(m) for m in rx.findall(text)}
        if found:
            out[kind] = found
    return out


def _read(path: Path) -> str | None:
    try:
        if not path.is_file() or SKIP_PARTS & set(path.parts):
            return None
        if path.stat().st_size > 8_000_000:
            return None
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


CRED_NAME = re.compile(r"^\s*([A-Z0-9_]*(?:TOKEN|KEY|SECRET|PASSWORD))\s*=\s*(\S+)\s*$")


def _blind_spots(text: str, matched: set[str]) -> list[str]:
    """Key names that LOOK like credentials but matched no pattern.

    Without this the scanner's clean verdict is worthless: an unrecognised
    shape reads exactly like an absence. The managed `.env` files are the
    control group -- every credential in them is known to be a credential, so
    anything there that no pattern catches is a hole in the pattern list, not
    a clean disk.
    """
    holes = []
    for line in text.splitlines():
        m = CRED_NAME.match(line)
        if not m:
            continue
        name, value = m.group(1), m.group(2).strip('"\'')
        if len(value) >= 20 and _digest(value) not in matched:
            holes.append(f"{name} (len {len(value)})")
    return holes


def _self_test() -> int:
    """Plant a fake stale credential where the sweep looks, and require a hit.

    A scanner that reports "clean" is only worth having if it can be shown to
    report "dirty". This writes a credential-SHAPED string that was never
    issued by anyone, confirms the sweep finds it and calls it stale, and
    removes it again.
    """
    root = next((r for r, _ in SWEEP if r.exists()), None)
    if root is None:
        print("SELF-TEST SKIPPED: no sweep root exists on this machine")
        return 0
    bait = root / "scan-self-test.tmp"
    # Shaped like a Telegram token; the digits are 1234567890, which is not one.
    bait.write_text("bot_token: 1234567890:" + "A" * 35 + "\n", encoding="utf-8")
    try:
        found = _matches(bait.read_text(encoding="utf-8"))
        ok = "telegram bot token" in found
        print(f"  planted {bait}")
        print(f"  detected: {sorted(found)}")
        print("SELF-TEST PASS: the sweep finds a planted credential"
              if ok else "SELF-TEST FAIL: planted credential not detected")
        return 0 if ok else 1
    finally:
        bait.unlink(missing_ok=True)


def main() -> int:
    if "--self-test" in sys.argv:
        return _self_test()
    delete = "--delete-stale" in sys.argv

    live: set[str] = set()
    holes: list[str] = []
    literals: dict[str, str] = {}          # exact value -> "<agent>:<NAME>"
    for path in IN_USE:
        text = _read(path)
        if not text:
            continue
        here: set[str] = set()
        for digests in _matches(text).values():
            here |= digests
        live |= here
        for line in text.splitlines():
            m = CRED_NAME.match(line)
            if m and m.group(1) in SHAPELESS:
                literals[m.group(2).strip('"\'')] = f"{path.parent.name}:{m.group(1)}"
        here |= {_digest(v) for v in literals}
        holes += [f"{path.parent.name}: {h}" for h in _blind_spots(text, here)]

    print(f"credentials in use: {len(live)} distinct value(s), across "
          f"{sum(1 for p in IN_USE if p.is_file())} managed file(s)")
    if holes:
        print("\nPATTERN BLIND SPOTS -- these look like credentials and match no")
        print("pattern, so a stale copy of one would NOT be found. Names only:")
        for h in holes:
            print(f"  ? {h}")
        print("Add its shape to PATTERNS before trusting a clean result.")
    print()

    stale_files: list[tuple[Path, dict]] = []
    live_copies: list[tuple[Path, dict]] = []
    seen: set[Path] = set()

    for root, pattern in SWEEP:
        if not root.exists():
            continue
        for path in sorted(root.glob(pattern)):
            if path in seen or path in IN_USE:
                continue
            seen.add(path)
            text = _read(path)
            if text is None:
                continue
            found = _matches(text)
            # Shapeless secrets: exact-match only, and always a live copy --
            # the value came from the file that is currently in use.
            for value, label in literals.items():
                if value in text:
                    found.setdefault(label, set()).add(_digest(value))
                    live.add(_digest(value))
            if not found:
                continue
            every = {d for ds in found.values() for d in ds}
            (live_copies if every & live else stale_files).append((path, found))

    if live_copies:
        print("UNMANAGED COPIES OF A CREDENTIAL THAT IS STILL IN USE")
        print("  Rotating will miss these. Decide on each one; not swept.")
        for path, found in live_copies:
            print(f"  ! {path}")
            for kind, ds in found.items():
                print(f"      {kind}: {', '.join(sorted(ds))}")
        print()

    if not stale_files:
        print("No stale credential copies on disk.")
        return 1 if live_copies else 0

    print(f"STALE -- {len(stale_files)} file(s) holding only rotated-out values")
    for path, found in stale_files:
        kinds = ", ".join(f"{k} x{len(v)}" for k, v in found.items())
        print(f"  {'deleting' if delete else 'found'}  {path}  ({path.stat().st_size:,}B)  -> {kinds}")
        if delete:
            try:
                path.unlink()
            except OSError as exc:
                print(f"      could not remove: {exc}")

    if not delete:
        print("\nRe-run with --delete-stale to remove them. A dead key that still")
        print("looks live is worse than no key: it gets tried before it gets checked.")
    return 0 if delete else 1


if __name__ == "__main__":
    sys.exit(main())
