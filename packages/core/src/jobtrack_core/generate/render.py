"""Rendering a tailored resume to PDF.

This is where contact details rejoin the document. They were kept on separate columns
and out of every prompt precisely so that this could be the only place they appear —
the model that wrote the bullets never saw the name at the top of the page.

Deliberately plain. One column, one typeface, generous margins, no colour. A resume is
read by an applicant tracking system before a person, and every column, table and text
box is a way for that system to read the page in the wrong order.
"""

from __future__ import annotations

from dataclasses import dataclass

from fpdf import FPDF

from jobtrack_core.generate.schemas import TailoredResume

#: Millimetres. Wide enough that a parser does not run text into the page edge.
MARGIN = 18.0
LINE = 5.0


@dataclass(frozen=True, slots=True)
class Contact:
    """What is re-attached at render time, and never sent to a model."""

    full_name: str | None = None
    email: str | None = None
    phone: str | None = None
    location: str | None = None
    links: tuple[str, ...] = ()

    def line(self) -> str:
        parts = [self.email, self.phone, self.location, *self.links]
        return "  ·  ".join(part for part in parts if part)


def render_pdf(resume: TailoredResume, contact: Contact, *, headline: str | None = None) -> bytes:
    """Render to PDF bytes.

    Helvetica rather than an embedded font: it is one of the fourteen faces every PDF
    reader has, so the file stays small and renders identically without shipping a
    font file whose licence would need checking.
    """
    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=MARGIN)
    pdf.set_margins(MARGIN, MARGIN, MARGIN)
    pdf.add_page()
    width = pdf.w - 2 * MARGIN

    if contact.full_name:
        pdf.set_font("Helvetica", "B", 17)
        pdf.multi_cell(width, 7.5, contact.full_name)
    if line := contact.line():
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(90, 90, 90)
        pdf.multi_cell(width, LINE, line)
        pdf.set_text_color(0, 0, 0)
    if headline:
        pdf.ln(1.5)
        pdf.set_font("Helvetica", "", 10)
        pdf.multi_cell(width, LINE, headline)

    if resume.summary:
        pdf.ln(3)
        pdf.set_font("Helvetica", "", 10)
        pdf.multi_cell(width, LINE, resume.summary)

    for section in resume.sections:
        if not section.bullets:
            continue
        pdf.ln(4)
        pdf.set_font("Helvetica", "B", 11)
        pdf.multi_cell(width, 5.5, section.heading)

        subtitle = " · ".join(part for part in (section.role,) if part and part != section.heading)
        if subtitle:
            pdf.set_font("Helvetica", "I", 9.5)
            pdf.set_text_color(90, 90, 90)
            pdf.multi_cell(width, LINE, subtitle)
            pdf.set_text_color(0, 0, 0)

        pdf.set_font("Helvetica", "", 10)
        for bullet in section.bullets:
            # An indented hanging bullet rather than a list: fpdf has no list
            # primitive, and a leading marker plus a narrower cell is what a parser
            # reads back as a bullet anyway.
            pdf.ln(0.8)
            pdf.set_x(MARGIN)
            pdf.cell(4, LINE, "-")
            pdf.multi_cell(width - 4, LINE, _latin1(bullet.text))

    return bytes(pdf.output())


def _latin1(text: str) -> str:
    """Fold characters the built-in fonts cannot encode.

    The fourteen standard PDF faces are Latin-1 only. An em dash or a curly quote from a
    pasted bullet would raise at render time, which would turn a typographic detail into
    a failed document, so they are folded to their ASCII equivalents instead.
    """
    # The keys are the characters being folded, so their "ambiguity" is the point.
    replacements = {
        "\u2014": "-",  # em dash
        "\u2013": "-",  # en dash
        "\u2018": "'",  # left single quote
        "\u2019": "'",  # right single quote
        "\u201c": '"',  # left double quote
        "\u201d": '"',  # right double quote
        "\u2026": "...",  # ellipsis
        "\u00a0": " ",  # non-breaking space
        "\u2022": "-",  # bullet
        "\u2192": "->",  # rightwards arrow
    }
    for source, target in replacements.items():
        text = text.replace(source, target)
    return text.encode("latin-1", errors="replace").decode("latin-1")
