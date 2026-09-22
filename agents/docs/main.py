#!/usr/bin/env python3
"""Docs agent -- turns an enquiry into a proposal PDF, as an MCP server.

WHAT IT IS FOR
--------------
W1: "proposal: <enquiry>" arrives on Telegram. Hermes gathers context (the
research agent for domain background, pm_query for how much similar work has
been done) and hands this agent the enquiry plus that context. It returns a
drafted proposal and a rendered PDF for Bukoma to read. Nothing is sent to a
client by this agent, ever -- see DELIVERY below.

IDENTITY IS CODE-AUTHORED, NOT MODEL-AUTHORED
---------------------------------------------
The author's name, the business name, the rate, engagement types, payment terms
and the signature block come from config/juma.json and are written into the
document by `proposal._render()`. They do not pass through the model, so a
proposal cannot be signed with the wrong name or quote a rate nobody set.

Then `invariants.check_proposal_invariants()` reads the finished text back and
asserts it against that same config -- name present, business present, any
stated hourly rate matching, no stale dateline. It exists because a previous
draft was signed "Alex Mercer", carried no business name and was datelined five
months stale, and passed every check that existed, because no check read the
content. A proposal that fails invariants is returned WITH its violations and
without a PDF: the draft is still visible, it just cannot be presented as a
document.

DELIVERY, AND WHY THERE IS NO BOT TOKEN HERE
--------------------------------------------
Hermes can attach files to a Telegram message natively (its Telegram adapter
has `send_document`, and its cron delivery documents media attachments such as
"a generated PDF"). So this agent returns a PATH and Hermes attaches it. No bot
token is in this process, no HTTP client points at Telegram, and the delivery
path is the same one every other Hermes message uses.

The PDF is written under agents/docs/output/ and nowhere else. The path is
built here from a slugged client name, never from anything a model produced.

DELIVERABLES GO TO BUKOMA ONLY
------------------------------
This agent has no send capability of any kind: no SMTP, no ClickUp client, no
HTTP client except the model provider's. "Send this to the client" is not a
thing it can be persuaded to do, because there is no code path that reaches a
client. That is the containment; the prompt rule is only a courtesy.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from _common import guard                      # noqa: E402
from _common.errors import AgentError, ok      # noqa: E402
from _common.server import bootstrap, fatal    # noqa: E402

import invariants                              # noqa: E402
import llm as llm_shim                         # noqa: E402
import proposal as proposal_mod                # noqa: E402
import proposal_pdf                            # noqa: E402

AGENT = "docs"
VERSION = "1.0.0"

OUTPUT_DIR = HERE / "output"

INSTRUCTIONS = """Proposal drafting for a freelance software engineer.

draft_proposal turns a client enquiry into a proposal and renders it as a PDF.
Identity, rates and the signature come from config/juma.json in code, never
from a model.

This agent cannot send anything to anyone. It returns a draft and a file path;
delivering that file is the orchestrator's job, and its only recipient is
Bukoma."""


def _slug(value: str, fallback: str = "client") -> str:
    """A filesystem-safe stem. Never used to escape OUTPUT_DIR: the result has
    no separators and no dots, and is joined to a fixed directory."""
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", str(value or "")).strip("-")
    return (cleaned[:48] or fallback).lower()


def main() -> None:
    try:
        boot = bootstrap(
            agent=AGENT,
            version=VERSION,
            instructions=INSTRUCTIONS,
            required=[],
            optional=["GROQ_API_KEY", "OPENROUTER_API_KEY"],
            default_model="openai/gpt-oss-120b",
        )
        llm_shim.configure(boot.llm)
        identity = invariants.load_identity()
        if not identity.get("name") or not identity.get("business"):
            raise RuntimeError(
                "config/juma.json is missing name or business; this agent "
                "cannot author a proposal it could not then verify")
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        fatal(AGENT, exc)
        return

    @boot.tool(
        name="draft_proposal",
        description=(
            "Draft a proposal for a client enquiry and render it as a PDF. "
            "Pass the enquiry verbatim, plus any research or project context "
            "already gathered. Identity, rates and signature come from "
            "config/juma.json, not from the model. Returns the draft text, the "
            "invariant check, and the PDF path for you to attach. It cannot "
            "send anything to the client."
        ),
        # Writes only a PDF into this agent's own output directory. It changes
        # nothing outside this machine, so it is not one of the manifest's
        # write_tools and does not need an approval prompt. The ClickUp task
        # that follows a proposal DOES -- that goes through pm_action.
        annotations={"readOnlyHint": True},
    )
    def draft_proposal(enquiry: str, context: str = "",
                       client_name: str = "") -> dict:
        enquiry = str(enquiry or "").strip()
        if not enquiry:
            raise AgentError("bad_input", "enquiry must not be empty")
        if len(enquiry) > llm_shim.MAX_ENQUIRY_CHARS:
            raise AgentError(
                "bad_input",
                f"enquiry is {len(enquiry)} characters, above the "
                f"{llm_shim.MAX_ENQUIRY_CHARS} limit; refusing rather than "
                f"truncating -- a truncated enquiry produces a confident "
                f"proposal for work the client did not describe.")

        boot.audit.write("draft_proposal_start", enquiry_chars=len(enquiry),
                         context_chars=len(context or ""),
                         client_name=client_name or None)

        # The enquiry is a client's words, forwarded. The gathered context came
        # from web pages and a task tracker other people can write to. Both are
        # material for the draft and neither is an instruction to this agent.
        fenced_context = (guard.wrap_untrusted(context, label="gathered context")
                          if (context or "").strip() else "")

        try:
            drafted = proposal_mod.synthesize_proposal(
                enquiry=enquiry,
                research_findings={"summary": fenced_context} if fenced_context else {},
                project_context={},
                coding_context={},
                config=identity,
            )
        except llm_shim.LLMError as exc:
            raise AgentError("draft_failed", str(exc), retryable=False) from None

        body = drafted.get("body") or drafted.get("text") or ""
        if not body.strip():
            raise AgentError("draft_failed", "the model produced an empty proposal")

        checked = invariants.check_proposal_invariants(drafted, body, identity)
        boot.audit.write("proposal_invariants", ok=checked["ok"],
                         violations=[v.get("field") for v in checked["violations"]])

        pdf_path = None
        pdf_error = None
        if not checked["ok"]:
            # Deliberate: a draft that contradicts config is shown, not
            # rendered. A PDF looks final, and a final-looking document with
            # the wrong name on it is the exact failure this gate exists for.
            pdf_error = ("not rendered: the draft failed its identity checks "
                         "(see invariants.violations)")
        elif not proposal_pdf.available():
            pdf_error = "fpdf2 is not installed; the draft text is unaffected"
        else:
            stem = _slug(client_name or drafted.get("client") or "client")
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            target = OUTPUT_DIR / f"proposal-{stem}-{stamp}.pdf"
            try:
                pdf_path = str(proposal_pdf.render(drafted, identity, target))
                boot.audit.write("proposal_pdf", path=pdf_path,
                                 bytes=target.stat().st_size)
            except Exception as exc:
                pdf_error = f"render failed: {type(exc).__name__}"
                boot.audit.write("proposal_pdf_failed", error=type(exc).__name__)

        return ok(
            proposal=body,
            invariants=checked,
            pdf_path=pdf_path,
            pdf_error=pdf_error,
            services=drafted.get("services"),
            client=drafted.get("client"),
            delivery=("Attach pdf_path to a Telegram message to Bukoma. This "
                      "agent cannot send anything, and the proposal must not "
                      "be sent to the client."),
            agent=AGENT, version=VERSION,
        )

    boot.run()


if __name__ == "__main__":
    main()
