#!/usr/bin/env python3
"""
Render an approved-shape proposal as a PDF document.

The PDF is an ADDITION. The plain-text body remains the canonical artefact --
it is what the approval prompt shows, what the evidence file stores, and what
check_proposal_invariants asserts against config. This module re-renders that
same content into a document a client can be handed.

IDENTITY AND FIGURES ARE CODE-AUTHORED, exactly as in the text body. name,
business, contact, rate, engagement types and payment terms are read from
config/juma.json here; none of them passes through a model, so a PDF cannot be
signed with the wrong name or quote a rate nobody set. The body text is parsed
back into blocks rather than re-generated, so the document and the text say the
same thing by construction.

fpdf2 is imported lazily. It is this repository's only Python dependency and it
pulls in three of its own (defusedxml, fonttools, Pillow). A machine without it
must still be able to draft and approve proposals, so a missing library yields
"no PDF attached", never "no proposal".
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestrator.proposal import normalise_punctuation  # noqa: E402

# Core-14 fonts need no font file and embed no glyphs beyond latin-1. The body
# is already ASCII by the time it reaches here (see normalise_punctuation), so
# there is nothing to embed and no font licensing to carry.
FONT = "Helvetica"
PAGE_MARGIN = 18
LINE_HEIGHT = 5.2


class PdfUnavailable(RuntimeError):
    """fpdf2 is not installed. The proposal itself is unaffected."""


def available() -> bool:
    try:
        import fpdf  # noqa: F401
        return True
    except Exception:
        return False


def _blocks(body: str):
    """Split the rendered body into (kind, text) blocks.

    Parsed from the text body rather than re-rendered from the model reply, so
    the document cannot drift from the text that was approved.
    """
    out = []
    for raw in body.splitlines():
        line = raw.rstrip()
        if not line.strip():
            out.append(("gap", ""))
        elif line.endswith(":") and len(line) < 60:
            out.append(("heading", line))
        elif re.match(r"^\s*[-*]\s+", line):
            out.append(("bullet", re.sub(r"^\s*[-*]\s+", "", line)))
        elif re.match(r"^\s*\d+\.\s+", line):
            out.append(("numbered", line.strip()))
        else:
            out.append(("text", line.strip()))
    return out


def render(proposal: dict, config: dict, out_path) -> Path:
    """Write the proposal to out_path as a PDF. Returns the path."""
    try:
        from fpdf import FPDF
    except Exception as e:  # pragma: no cover - exercised by the availability test
        raise PdfUnavailable(f"fpdf2 is not installed: {e}")

    body = normalise_punctuation(proposal.get("body") or "")
    subject = normalise_punctuation(str(proposal.get("subject") or "Proposal"))

    # Identity, from config only. Never from the model.
    name = str(config.get("name") or "")
    business = str(config.get("business") or "")
    contact = config.get("contact") or {}
    email = str(contact.get("email") or "")
    telegram = str(contact.get("telegram") or "")

    pdf = FPDF(format="A4", unit="mm")
    pdf.set_auto_page_break(auto=True, margin=PAGE_MARGIN)
    pdf.set_margins(PAGE_MARGIN, PAGE_MARGIN, PAGE_MARGIN)
    pdf.set_title(subject)
    if name:
        pdf.set_author(name)
    pdf.add_page()
    width = pdf.w - 2 * PAGE_MARGIN

    # Letterhead -- code-authored identity.
    if business:
        pdf.set_font(FONT, "B", 15)
        pdf.multi_cell(width, 7, business)
    line = " | ".join(x for x in (name, email, telegram) if x)
    if line:
        pdf.set_font(FONT, "", 9)
        pdf.set_text_color(90, 90, 90)
        pdf.multi_cell(width, 5, line)
        pdf.set_text_color(0, 0, 0)
    pdf.ln(3)
    pdf.set_draw_color(180, 180, 180)
    pdf.line(PAGE_MARGIN, pdf.get_y(), pdf.w - PAGE_MARGIN, pdf.get_y())
    pdf.ln(5)

    pdf.set_font(FONT, "B", 12)
    pdf.multi_cell(width, 6, subject)
    pdf.ln(3)

    # The signature block is already at the end of the text body; rendering it
    # again here would print the identity twice.
    trimmed = []
    for kind, text in _blocks(body):
        if kind == "text" and text.strip() == "Regards,":
            break
        trimmed.append((kind, text))

    for kind, text in trimmed:
        if kind == "gap":
            pdf.ln(2.5)
        elif kind == "heading":
            pdf.ln(1)
            pdf.set_font(FONT, "B", 10.5)
            pdf.multi_cell(width, LINE_HEIGHT + 0.6, text)
        elif kind == "bullet":
            pdf.set_font(FONT, "", 10)
            pdf.multi_cell(width, LINE_HEIGHT, chr(149) + "  " + text)
        elif kind == "numbered":
            pdf.set_font(FONT, "", 10)
            pdf.multi_cell(width, LINE_HEIGHT, text)
        else:
            pdf.set_font(FONT, "", 10)
            pdf.multi_cell(width, LINE_HEIGHT, text)

    # Signature, from config.
    pdf.ln(6)
    pdf.set_font(FONT, "", 10)
    pdf.multi_cell(width, LINE_HEIGHT, "Regards,")
    pdf.set_font(FONT, "B", 10)
    if name:
        pdf.multi_cell(width, LINE_HEIGHT, name)
    pdf.set_font(FONT, "", 10)
    for value in (business, email, telegram):
        if value:
            pdf.multi_cell(width, LINE_HEIGHT, value)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(out_path))
    return out_path


def filename_for(proposal: dict, config: dict) -> str:
    """A filename a client can receive without it looking machine-generated."""
    business = str(config.get("business") or "proposal")
    slug = re.sub(r"[^A-Za-z0-9]+", "-", business).strip("-")[:40] or "proposal"
    rid = str(proposal.get("proposal_id") or "")[:8]
    return f"Proposal-{slug}-{rid}.pdf" if rid else f"Proposal-{slug}.pdf"
