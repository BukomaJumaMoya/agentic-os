#!/usr/bin/env python3
"""
Proposal synthesis.

The prose comes from a model; the identity does not. name, business and contact
are appended by code after the model returns, so a proposal cannot be signed
with anyone else's name regardless of what the model emits. That makes the
"Alex Mercer" failure structurally impossible rather than merely detected --
check_proposal_invariants remains the final gate before send, as a second line
of defence rather than the only one.

There is no template fallback. If the model is unavailable or returns something
that fails validation, no proposal object is produced at all.
"""

import json
import re
import sys
import uuid
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestrator import llm as _llm  # noqa: E402

# v2: subject is model-authored and validated; service justifications must cite
# the client's own words; technical choices must be framed as options; no
# speculation about unstated requirements. Bumped because a proposal drafted
# under v1 is not comparable to one drafted under v2 -- evidence records which.
PROMPT_VERSION = "proposal/v2"


def _reply_schema(allowed_services):
    """JSON Schema for the model reply, for providers that constrain decoding.

    Strict mode requires every property listed in ``required`` and
    ``additionalProperties: false`` on every object, so this mirrors the reply
    contract in SYSTEM_PROMPT exactly. The service enum is built from the
    candidate list, which makes an unoffered service unrepresentable on a
    constrained decoder -- but _validate still rejects one, because the other
    providers are not constrained and must be held to the same standard.
    """
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["subject", "understanding", "services", "approach",
                     "clarifications", "closing"],
        "properties": {
            # No maxLength here: Groq's strict mode accepts a subset of JSON
            # Schema and an unsupported keyword is a 400, which the chain would
            # report as a cold provider. The length rule lives in _validate,
            # where it applies to every provider anyway.
            "subject": {"type": "string"},
            "understanding": {"type": "string"},
            "services": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["service", "why"],
                    "properties": {
                        "service": {"type": "string",
                                    "enum": list(allowed_services)},
                        "why": {"type": "string"},
                    },
                },
            },
            "approach": {"type": "array", "items": {"type": "string"}},
            "clarifications": {"type": "array", "items": {"type": "string"}},
            "closing": {"type": "string"},
        },
    }

# Stage 1 of service matching: deterministic, debuggable, and it produces the
# candidate set the model is allowed to choose from. Keys must match entries in
# config["services"] exactly.
SERVICE_KEYWORDS = {
    "AI agentic systems": {
        "ai", "agent", "agentic", "llm", "chatbot", "automation", "intelligent",
        "machine learning", "gpt", "assistant",
    },
    "Web applications": {
        "web", "website", "webapp", "web app", "portal", "dashboard", "frontend",
        "browser", "online system", "platform",
    },
    "APIs and integrations": {
        "api", "integration", "integrate", "webhook", "connect", "sync",
        "third party", "third-party", "import", "export", "spreadsheet", "excel",
        "csv", "migrate",
    },
    "ClickUp automation": {
        "clickup", "task", "project management", "workflow", "ticket", "kanban",
        "scheduling", "appointment", "reminder", "booking",
    },
    "Telegram bot development": {
        "telegram", "bot", "whatsapp", "sms", "messaging", "message", "chat",
        "notification", "notify", "reminder", "broadcast", "communicate",
        "communication",
    },
    "Technical research": {
        "research", "evaluate", "compare", "feasibility", "options", "advice",
        "recommend", "assessment", "audit",
    },
}


def match_services(enquiry: str, services: list) -> list:
    """Stage 1: score configured services by keyword hits in the enquiry.

    Returns [{"service", "score", "matched"}] ordered by score, restricted to
    services that actually appear in config. Never invents a service.
    """
    text = (enquiry or "").lower()
    scored = []
    for service in services or []:
        terms = SERVICE_KEYWORDS.get(service, set())
        matched = sorted(t for t in terms if t in text)
        if matched:
            scored.append({"service": service, "score": len(matched), "matched": matched})
    scored.sort(key=lambda s: (-s["score"], s["service"]))
    return scored


SYSTEM_PROMPT = """\
You are drafting the body of a first-response proposal for an independent \
software engineering practice, replying to an inbound client enquiry.

You are writing on behalf of a real person to a real prospective client. \
Accuracy matters more than polish, and an honest gap is better than a \
confident invention.

HARD CONSTRAINTS -- these are not style preferences:

1. Never state a name, business name, signature or contact detail. The \
signature is attached separately by the system. Do not write "Regards" or any \
closing.
2. Never offer a service that is not in the CANDIDATE SERVICES list you are \
given. That list is the complete set of things this practice does for this \
enquiry. If none fit, return an empty services array.
3. Never state a price, rate, budget figure, discount or payment schedule. You \
do not know them. If the client mentioned a budget, you may acknowledge that \
they mentioned one without quoting or endorsing a number.
4. Never state a delivery date, deadline or duration as a commitment. You may \
describe a sequence of phases without attaching times to them.
5. Never invent facts about the client, their company, their industry, their \
size, their current systems or their customers. Use only what the enquiry \
states. Where something important is unstated, put it in "clarifications" \
rather than assuming it.
6. Never claim work has begun, that you have researched them, or that anything \
has been verified. No external research was performed.
7. Do not use placeholder markers of any kind -- no [CLIENT], no TODO, no \
"Lorem", no XXX, no fill-in-the-blank brackets.
8. Every service justification must cite something the client actually wrote. \
Quote their words or paraphrase them closely. If they described a need without \
naming a specific technology, your justification must reflect what they \
actually said, not a technology you inferred.
   WRONG: "Telegram bot development -- Telegram is a common channel" \
(the client never mentioned Telegram; this justifies the service by an \
assumption about them).
   RIGHT: "Telegram bot development -- you asked to reach clients across \
multiple platforms without naming which ones, and this is one of the channels \
I build for".
9. Never present a technical choice as already made. Tools, languages, \
platforms, hosting and architecture are options to confirm with the client, \
not decisions you have taken on their behalf.
   WRONG: "Test the end-to-end flow and deploy in a containerized environment" \
(containers were never discussed and are presented as settled).
   RIGHT: "Agree a deployment target -- containers are one option -- and test \
the end-to-end flow against it".
10. Never speculate about a requirement the client did not state. If you catch \
yourself writing "you may want", "presumably", "likely you need" or "if you \
also need", that belongs in "clarifications" as a question, not in \
"understanding" or "approach" as a statement.
   WRONG (in approach): "Create a lightweight web app for managing contacts, \
since you may want a simple interface".
   RIGHT (in clarifications): "Do you need a screen for staff to manage \
contacts, or is the spreadsheet staying as the place that data is edited?"

TONE: plain, direct, competent. Short sentences. No marketing language, no \
superlatives, no "cutting-edge", "seamless", "robust", "leverage", or \
"delighted". Address the client as "you". Write as a practitioner who has read \
their problem, not a vendor pitching.

Restate their problem in your own words before proposing anything, using their \
specifics. If they mentioned a spreadsheet, appointment reminders and two \
messaging channels, your understanding section must show those. A reply that \
would fit any other enquiry has failed.

Return ONLY a JSON object, no prose around it, with exactly these keys:

{
  "subject": "one line, AT MOST 60 characters, naming this client's actual problem. Count them. No 'Re:' prefix, no company boilerplate, no line breaks",
  "understanding": "2-4 sentences restating their situation and what they need, in their specifics",
  "services": [
    {"service": "<exact string from CANDIDATE SERVICES>",
     "why": "one clause citing what the client actually wrote -- see constraint 8"}
  ],
  "approach": ["3-5 short phases, each one sentence, no dates or durations, no settled technical choices -- see constraint 9"],
  "clarifications": ["2-4 specific questions you genuinely need answered to scope this"],
  "closing": "1-2 sentences on what happens next. No signature, no dates, no prices."
}
"""

USER_PROMPT_TEMPLATE = """\
CLIENT ENQUIRY (verbatim, do not assume anything beyond it):
\"\"\"
{enquiry}
\"\"\"

CANDIDATE SERVICES (choose only from these; ordered by keyword relevance):
{candidates}

TECHNICAL STACK THIS PRACTICE USES (ground any technical suggestion in these; \
do not present them as already chosen):
{tech_stack}

ENGAGEMENT TYPES AVAILABLE (you may mention that options exist; do not quote \
prices or recommend one):
{engagement_types}

Draft the proposal body as the JSON object described. Remember: no name, no \
signature, no prices, no dates, no invented facts.
"""

_PLACEHOLDER_RE = re.compile(
    r"\[(?:client|company|name|date|price|todo|insert|xxx)[^\]]*\]|\bTODO\b|\bLorem\b|\bXXX\b",
    re.IGNORECASE)
# Any currency amount at all: the model is told never to quote one.
_MONEY_RE = re.compile(r"[$£€]\s*\d|(?<!\w)\d+\s*(?:USD|EUR|GBP)(?!\w)", re.IGNORECASE)


def _validate(data: dict, allowed_services: set) -> list:
    """Return a list of validation problems; empty means acceptable."""
    problems = []

    subject = (data.get("subject") or "").strip()
    if not subject:
        problems.append("subject is missing")
    elif "\n" in subject or "\r" in subject:
        problems.append("subject must be a single line")
    elif len(subject) > 70:
        # The prompt asks for 60; the gate is 70. Models routinely graze a
        # stated ceiling by a few characters, and discarding an otherwise valid
        # proposal over three of them is not a useful failure. Beyond 70 the
        # instruction was ignored rather than approximated, which is worth
        # failing on -- nothing here is silently trimmed.
        problems.append(
            f"subject is {len(subject)} characters, above the 70 character limit")
    elif len(subject) < 10:
        problems.append("subject is too short to name the problem")

    understanding = (data.get("understanding") or "").strip()
    if len(understanding) < 40:
        problems.append("understanding is missing or too short to be specific")

    closing = (data.get("closing") or "").strip()
    if not closing:
        problems.append("closing is missing")

    services = data.get("services")
    if not isinstance(services, list):
        problems.append("services must be a list")
        services = []
    for entry in services:
        if not isinstance(entry, dict):
            problems.append(f"service entry is not an object: {entry!r}")
            continue
        name = (entry.get("service") or "").strip()
        if name not in allowed_services:
            problems.append(f"service not offered by this practice: {name!r}")
        if not (entry.get("why") or "").strip():
            problems.append(f"service {name!r} has no justification")

    approach = data.get("approach")
    if not isinstance(approach, list) or not approach:
        problems.append("approach must be a non-empty list")

    clarifications = data.get("clarifications")
    if not isinstance(clarifications, list) or not clarifications:
        problems.append("clarifications must be a non-empty list")

    blob = json.dumps(data)
    if _PLACEHOLDER_RE.search(blob):
        problems.append("reply contains placeholder markers")
    if _MONEY_RE.search(blob):
        problems.append("reply quotes a currency amount, which it must never do")

    return problems


# Characters models emit freely that survive badly downstream. Telegram's
# plain-text mode, cp1252 mail clients and the Windows console each mangle a
# different subset, and a non-breaking hyphen is invisible in review but breaks
# a line the reader never sees broken. Normalise once, at render, so the stored
# body is exactly the delivered body.
_PUNCTUATION = {
    "\u2010": "-",    # hyphen
    "\u2011": "-",    # non-breaking hyphen  <- the one gpt-oss-120b emitted
    "\u2012": "-",    # figure dash
    "\u2013": "-",    # en dash              <- present in config default_rate
    "\u2014": " - ",  # em dash
    "\u2015": "-",    # horizontal bar
    "\u2018": "'",    # left single quote
    "\u2019": "'",    # right single quote / apostrophe
    "\u201a": "'",
    "\u201b": "'",
    "\u201c": '"',    # left double quote
    "\u201d": '"',    # right double quote
    "\u201e": '"',
    "\u2026": "...",  # ellipsis
    "\u00a0": " ",    # non-breaking space
    "\u202f": " ",    # narrow no-break space
    "\u2009": " ",    # thin space
    "\u200b": "",     # zero-width space
    "\ufeff": "",     # byte order mark
}


def normalise_punctuation(text: str) -> str:
    """Fold typographic punctuation to ASCII for email and Telegram delivery."""
    if not text:
        return text
    for source, target in _PUNCTUATION.items():
        text = text.replace(source, target)
    return text


def _commercial_block(config: dict) -> list:
    """Commercial terms, built entirely in code from config.

    The model is forbidden to state any figure (constraint 3) and none of this
    passes through it. Every value here is read from config/juma.json, so a rate
    can only be wrong if the config is wrong -- a model cannot invent, round or
    helpfully discount one.

    Framed as indicative rather than as a quote, which is the honest position:
    the clarifications above are unanswered, so the scope is genuinely unknown.
    Pricing work you cannot yet scope is how a fixed price becomes a loss.
    """
    rate = config.get("default_rate")
    currency = config.get("currency")
    engagements = config.get("engagement_types") or []
    terms = config.get("payment_terms")
    sla = config.get("proposal_sla")

    if not any([rate, engagements, terms, sla]):
        return []

    lines = ["Commercial terms (indicative, not a quote):"]
    if rate:
        suffix = " (" + currency + ")" if currency else ""
        lines.append("- Indicative rate: " + str(rate) + suffix)
    if engagements:
        lines.append("- Engagement options: " + ", ".join(engagements))
    if terms:
        lines.append("- Payment terms: " + str(terms))
    lines.append("")
    firm = ("The rate above is indicative and depends on the answers to the "
            "questions above. Once scope is agreed I will send a firm, scoped "
            "quote")
    lines.append(firm + (" within " + str(sla) + "." if sla else "."))
    return lines


def _render(data: dict, config: dict) -> str:
    """Assemble the final body. Identity is added here, never by the model."""
    lines = ["Hi,", "", "Thank you for reaching out.", ""]
    lines += [data["understanding"].strip(), ""]

    services = [s for s in (data.get("services") or []) if isinstance(s, dict)]
    if services:
        lines.append("How I can help:")
        for s in services:
            lines.append(f"- {s['service'].strip()} — {s['why'].strip()}")
        lines.append("")

    approach = data.get("approach") or []
    if approach:
        lines.append("Suggested approach:")
        lines += [f"{i}. {str(step).strip()}" for i, step in enumerate(approach, 1)]
        lines.append("")

    clarifications = data.get("clarifications") or []
    if clarifications:
        lines.append("To scope this accurately I need to know:")
        lines += [f"- {str(q).strip()}" for q in clarifications]
        lines.append("")

    lines += [data["closing"].strip(), ""]

    sla = config.get("response_sla")
    if sla:
        lines += [f"Response SLA: {sla}", ""]

    commercial = _commercial_block(config)
    if commercial:
        lines += commercial + [""]

    # Identity block -- code-authored, never model-authored.
    lines.append("Regards,")
    lines.append(config.get("name", ""))
    lines.append(config.get("business", ""))
    contact = config.get("contact") or {}
    for field in ("email", "telegram"):
        if contact.get(field):
            lines.append(str(contact[field]))

    return normalise_punctuation("\n".join(lines).strip()) + "\n"


def synthesize_proposal(enquiry: str, research_findings: dict, project_context: dict,
                        coding_context: dict, config: dict) -> dict:
    """Draft a proposal body for an enquiry.

    Raises ConfigError when no API key is configured, and LLMError when the
    model is unavailable or its reply fails validation. Never returns a
    template: callers get a proposal or an exception.

    research_findings / project_context / coding_context are recorded as
    attempted evidence but do not shape the prose -- the research pipeline
    currently produces nothing, and a proposal must not imply otherwise.
    """
    enquiry = (enquiry or "").strip()
    if not enquiry:
        raise _llm.LLMError("cannot draft a proposal from an empty enquiry",
                            kind="invalid_output")
    if len(enquiry) > _llm.MAX_ENQUIRY_CHARS:
        raise _llm.LLMError(
            f"enquiry is {len(enquiry)} characters, above the "
            f"{_llm.MAX_ENQUIRY_CHARS} limit; refusing rather than truncating",
            kind="invalid_output")

    services = config.get("services") or []
    candidates = match_services(enquiry, services)
    # Nothing matched by keyword: let the model choose from everything on offer
    # rather than silently proposing nothing.
    candidate_names = [c["service"] for c in candidates] or list(services)

    user_prompt = USER_PROMPT_TEMPLATE.format(
        enquiry=enquiry,
        candidates="\n".join(f"- {name}" for name in candidate_names) or "- (none)",
        tech_stack=", ".join(config.get("tech_stack") or []) or "(unspecified)",
        engagement_types=", ".join(config.get("engagement_types") or []) or "(unspecified)",
    )

    result = _llm.complete_json(SYSTEM_PROMPT, user_prompt,
                                schema=_reply_schema(candidate_names))
    data = result["data"]

    problems = _validate(data, set(services))
    if problems:
        raise _llm.LLMError(
            "model reply failed proposal validation: " + "; ".join(problems),
            kind="invalid_output", detail=json.dumps(data)[:600])

    body = _render(data, config)
    return {
        "proposal_id": str(uuid.uuid4()),
        "created_at": datetime.now(timezone.utc).isoformat(),
        # Model-authored and validated, not a slice of the enquiry. The old
        # f"Re: {enquiry[:120]}" cut mid-word and read as a machine quoting the
        # client back at themselves.
        "subject": data["subject"].strip(),
        "body": body,
        "config_used": {
            "name": config.get("name"),
            "business": config.get("business"),
            "default_rate": config.get("default_rate"),
        },
        "generation": {
            # Which provider served this, not just which model answered. With a
            # fallback chain, "gemini-2.5-flash" alone does not say whether the
            # first choice was cold when this proposal was drafted.
            "provider": result.get("provider"),
            "provider_chain": result.get("attempts"),
            "model": result["model"],
            "prompt_version": PROMPT_VERSION,
            "usage": result.get("usage"),
            # The parsed object is what was rendered; raw_text is what the model
            # actually emitted. Keep both -- when a proposal is questioned later,
            # "what did it say" must be answerable without a re-run.
            "raw_text": result.get("text"),
            "candidates": candidates,
            "response": data,
        },
        "evidence": {
            "research_findings": research_findings,
            "project_context": project_context,
            "coding_context": coding_context,
        },
    }
