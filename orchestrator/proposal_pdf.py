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
PAGE_MARGIN = 18          # mm, all four sides
LINE_HEIGHT = 5.2         # mm per line of 10pt body text
BULLET_INDENT = 6.0       # mm the text of a list item is inset by
FOOTER_HEIGHT = 12.0      # mm reserved at the bottom of every page

# A list marker is drawn in its own fixed-width cell and the text in a cell to
# its right, so a wrapped line aligns under the text rather than returning to
# the left margin. Previously a bullet that wrapped put its continuation back at
# the margin, which destroys the list structure and reads like the line was cut.
#
# The marker is "-" and not chr(149). 0x95 is an UNASSIGNED control byte in
# latin-1, which is the encoding a core font uses here; readers show it as a
# box, a blank, or drop it. A hyphen is in every encoding and every font.
BULLET_MARKER = "-"


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
            m = re.match(r"^\s*(\d+)\.\s+(.*)$", line)
            out.append(("numbered", (m.group(1) + ".", m.group(2))))
        else:
            out.append(("text", line.strip()))
    return out


def _break_long_tokens(pdf, text, width):
    """Hard-break any single token too wide to fit on a line.

    Word wrapping cannot break a token with no spaces in it, so one long
    unbroken string -- a URL, a path, a hash -- runs past the right edge no
    matter how the cell is sized. This is the only way text can overflow the
    content width, so it is removed at the source rather than hoped against.
    """
    out = []
    for token in text.split(" "):
        if pdf.get_string_width(token) <= width:
            out.append(token)
            continue
        piece = ""
        for ch in token:
            if pdf.get_string_width(piece + ch) > width and piece:
                out.append(piece)
                piece = ch
            else:
                piece += ch
        if piece:
            out.append(piece)
    return " ".join(out)


def _para(pdf, text, width, size=10, style="", indent=0.0):
    """One wrapped paragraph, inset by `indent`."""
    pdf.set_font(FONT, style, size)
    pdf.set_x(pdf.l_margin + indent)
    usable = width - indent
    pdf.multi_cell(usable, LINE_HEIGHT, _break_long_tokens(pdf, text, usable))


def _list_item(pdf, marker, text, width, marker_width):
    """A list item whose wrapped lines hang under the text, not the marker."""
    pdf.set_font(FONT, "", 10)
    y = pdf.get_y()
    pdf.set_xy(pdf.l_margin, y)
    # Marker in its own cell; no line break, so the text cell starts beside it.
    pdf.cell(marker_width, LINE_HEIGHT, marker)
    usable = width - marker_width
    # multi_cell continuation lines align to this cell's left edge, which is
    # where the text starts -- that is the hanging indent.
    pdf.multi_cell(usable, LINE_HEIGHT, _break_long_tokens(pdf, text, usable))
    pdf.set_x(pdf.l_margin)


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

    class _Doc(FPDF):
        def footer(self):
            self.set_y(-FOOTER_HEIGHT)
            self.set_font(FONT, "", 8)
            self.set_text_color(130, 130, 130)
            self.cell(0, 6, f"{business} - page {self.page_no()}", align="C")
            self.set_text_color(0, 0, 0)

    pdf = _Doc(format="A4", unit="mm")
    pdf.set_margins(PAGE_MARGIN, PAGE_MARGIN, PAGE_MARGIN)
    pdf.set_auto_page_break(auto=True, margin=FOOTER_HEIGHT + 6)
    pdf.set_title(subject)
    if name:
        pdf.set_author(name)
    pdf.add_page()

    # Every cell is sized from epw, the real usable width between the margins,
    # rather than a width computed by hand that can disagree with them.
    width = pdf.epw

    if business:
        _para(pdf, business, width, size=15, style="B")
    line = " | ".join(x for x in (name, email, telegram) if x)
    if line:
        pdf.set_text_color(90, 90, 90)
        _para(pdf, line, width, size=9)
        pdf.set_text_color(0, 0, 0)
    pdf.ln(2)
    pdf.set_draw_color(180, 180, 180)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
    pdf.ln(5)

    _para(pdf, subject, width, size=12, style="B")
    pdf.ln(2)

    # The signature block already ends the text body; rendering it again would
    # print the identity twice.
    blocks = []
    for kind, text in _blocks(body):
        if kind == "text" and str(text).strip() == "Regards,":
            break
        blocks.append((kind, text))

    pdf.set_font(FONT, "", 10)
    marker_width = pdf.get_string_width("00.") + 2.0
    for kind, text in blocks:
        if kind == "gap":
            pdf.ln(2.2)
        elif kind == "heading":
            pdf.ln(1.5)
            _para(pdf, text, width, size=10.5, style="B")
            pdf.ln(0.8)
        elif kind == "bullet":
            _list_item(pdf, BULLET_MARKER, text, width, BULLET_INDENT)
        elif kind == "numbered":
            number, rest = text
            _list_item(pdf, number, rest, width, marker_width)
        else:
            _para(pdf, text, width)

    pdf.ln(5)
    _para(pdf, "Regards,", width)
    if name:
        _para(pdf, name, width, style="B")
    for value in (business, email, telegram):
        if value:
            _para(pdf, value, width)

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
