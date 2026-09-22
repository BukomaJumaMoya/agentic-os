#!/usr/bin/env python3
"""The final content gate on a proposal, ported verbatim from
archive/pre-hermes/orchestrator/external_action.py on 2026-09-22.

WHY IT EXISTS, IN ITS OWN WORDS
-------------------------------
    Identity is the thing a drafting model gets wrong silently. The archived
    Acme draft was signed "Alex Mercer", carried no business name, and was
    datelined five months stale -- and passed every check that existed,
    because no check looked at the content at all.

So this reads the finished text back and asserts it against config/juma.json:
the author's name and business must appear, any stated hourly rate must match,
and a dateline must not be stale. It fails CLOSED -- an unreadable config
cannot authorise anything.

It is the second line, not the first. Identity, rates and the signature are
written into the document by code in proposal.py and never pass through a
model. This checks that what came out still says what code put in.

Ported unchanged except for the import block.
"""

from __future__ import annotations

import re
import json
from datetime import datetime, timezone
from pathlib import Path

IDENTITY = Path(__file__).resolve().parent.parent.parent / "config" / "juma.json"


def load_identity() -> dict:
    """config/juma.json, or {} -- which check_proposal_invariants treats
    as unverifiable and therefore refuses."""
    try:
        return json.loads(IDENTITY.read_text(encoding="utf-8"))
    except Exception:
        return {}

_DATE_TOLERANCE_DAYS = 2

_MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], start=1)}

_TEXT_DATE_RE = re.compile(
    r"\b(" + "|".join(_MONTHS) + r")\s+(\d{1,2}),?\s+(\d{4})\b", re.IGNORECASE)

_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")

_DATELINE_RE = re.compile(r"^\s*\**\s*date\s*\**\s*:", re.IGNORECASE)

_HOURLY_RATE_RE = re.compile(
    r"\$\s*[\d,]+(?:\s*[-–—]\s*\$?\s*[\d,]+)?\s*(?:/|\s*per\s+)\s*(?:hr|hour)",
    re.IGNORECASE)

def _digits(value: str) -> str:
    return re.sub(r"\D", "", value or "")

def _check_date(violations, line, month, day, year, reference):
    try:
        found = datetime(year, month, day, tzinfo=timezone.utc)
    except ValueError:
        return
    drift = abs((found - reference).days)
    if drift > _DATE_TOLERANCE_DAYS:
        violations.append({
            "field": "date",
            "expected": reference.date().isoformat(),
            "found": found.date().isoformat(),
            "detail": f"dateline is {drift} days from the proposal date: {line.strip()[:80]}",
        })

def check_proposal_invariants(proposal: dict, text: str, config: dict = None) -> dict:
    """Fail closed on proposal content that contradicts config/juma.json.

    Identity is the thing a drafting model gets wrong silently. The archived
    Acme draft was signed "Alex Mercer", carried no business name, and was
    datelined five months stale -- and passed every check that existed, because
    no check looked at the content at all.

    Returns {"ok": bool, "violations": [{field, expected, found, detail}]}.
    """
    # The orchestrator's approval store is gone; identity comes straight
    # from config/juma.json. A missing config still fails closed below.
    config = load_identity() if config is None else config
    violations = []
    body = text or ""

    # An unreadable config cannot authorise a send.
    expected_name = (config.get("name") or "").strip()
    expected_business = (config.get("business") or "").strip()
    if not expected_name or not expected_business:
        violations.append({
            "field": "config", "expected": "name and business in config/juma.json",
            "found": None,
            "detail": "cannot verify proposal identity against an empty or unreadable config",
        })
        return {"ok": False, "violations": violations}

    lowered = body.lower()
    if expected_name.lower() not in lowered:
        violations.append({"field": "name", "expected": expected_name, "found": None,
                           "detail": "author name from config does not appear in the proposal"})
    if expected_business.lower() not in lowered:
        violations.append({"field": "business", "expected": expected_business, "found": None,
                           "detail": "business name from config does not appear in the proposal"})

    # Rate: conditional. Generated proposals carry no rate line, so this fires
    # only when the text states an hourly rate -- which must then match config.
    expected_rate = (config.get("default_rate") or "").strip()
    found_rates = _HOURLY_RATE_RE.findall(body) or []
    if found_rates and expected_rate:
        want = _digits(expected_rate)
        for found in found_rates:
            if _digits(found) != want:
                violations.append({"field": "default_rate", "expected": expected_rate,
                                   "found": found.strip(),
                                   "detail": "stated hourly rate does not match config"})

    # Date: only the dateline / header block, not milestone dates in the body.
    reference = datetime.now(timezone.utc)
    created = proposal.get("created_at")
    if created:
        try:
            reference = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
        except Exception:
            pass
    lines = body.splitlines()
    header = [ln for i, ln in enumerate(lines) if i < 15 or _DATELINE_RE.match(ln)]
    for line in header:
        for month, day, year in _TEXT_DATE_RE.findall(line):
            _check_date(violations, line,
                        _MONTHS[month.lower()], int(day), int(year), reference)
        for year, month, day in _ISO_DATE_RE.findall(line):
            _check_date(violations, line, int(month), int(day), int(year), reference)

    return {"ok": not violations, "violations": violations}
