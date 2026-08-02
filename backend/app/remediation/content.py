"""Content-stream segmentation and marked-content tagging.

Walks a page's content stream instructions and splits them into spans:
  - text spans (BT ... ET) that contain a text-showing operator
  - image spans (a single `Do` invoking an Image XObject)
  - everything else (state-setting ops, vector drawing, Form XObjects,
    empty BT/ET blocks) which is treated as non-structural artifact content

Text spans are classified as headings (H1-H6) or paragraphs (P) using the
largest font size (from `Tf`) seen inside the span, relative to the
document's most common ("body") font size. Image spans become Figure
elements with alt text. Artifact spans are wrapped in `/Artifact BMC/EMC`
so no page content is left untagged, without needing a struct element.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

import pikepdf
from pikepdf import Dictionary, Name

TEXT_SHOW_OPS = {"Tj", "TJ", "'", '"'}
HEADING_TAGS = ["/H1", "/H2", "/H3", "/H4", "/H5", "/H6"]


@dataclass
class Span:
    kind: str  # "text" | "image" | "artifact"
    start: int
    end: int  # exclusive
    font_size: float | None = None
    xobject_name: str | None = None
    xobject: object | None = None
    has_text: bool = False


@dataclass
class PageAnalysis:
    spans: list = field(default_factory=list)
    body_size: float | None = None
    heading_sizes: list = field(default_factory=list)
    image_names: list = field(default_factory=list)
    has_any_text: bool = False


def _font_size_before(instructions) -> list:
    """`font_before[i]` = font size (from the last `Tf`) in effect just before
    instruction i. Font size is part of text state, which in practice
    (and per how most PDF writers emit it) persists across BT/ET boundaries,
    so this is tracked as one running value across the whole page rather
    than being reset inside each text object."""
    font_before = [None] * (len(instructions) + 1)
    current = None
    for idx, instr in enumerate(instructions):
        font_before[idx] = current
        if str(instr.operator) == "Tf":
            try:
                current = float(instr.operands[1])
            except (IndexError, TypeError, ValueError):
                pass
    font_before[len(instructions)] = current
    return font_before


def _segment(instructions) -> list[Span]:
    spans: list[Span] = []
    i = 0
    n = len(instructions)
    artifact_start = None
    font_before = _font_size_before(instructions)

    def flush_artifact(end):
        nonlocal artifact_start
        if artifact_start is not None and end > artifact_start:
            spans.append(Span(kind="artifact", start=artifact_start, end=end))
        artifact_start = None

    while i < n:
        op = str(instructions[i].operator)
        if op == "BT":
            flush_artifact(i)
            start = i
            j = i + 1
            font_size = font_before[start]
            has_text = False
            while j < n and str(instructions[j].operator) != "ET":
                jop = str(instructions[j].operator)
                if jop == "Tf":
                    try:
                        size = float(instructions[j].operands[1])
                        font_size = size if font_size is None else max(font_size, size)
                    except (IndexError, TypeError, ValueError):
                        pass
                elif jop in TEXT_SHOW_OPS:
                    has_text = True
                j += 1
            end = min(j + 1, n)  # include ET
            spans.append(
                Span(kind="text" if has_text else "artifact", start=start, end=end,
                     font_size=font_size, has_text=has_text)
            )
            i = end
            continue
        elif op == "Do":
            flush_artifact(i)
            name = str(instructions[i].operands[0]) if instructions[i].operands else None
            spans.append(Span(kind="image", start=i, end=i + 1, xobject_name=name))
            i += 1
            continue
        else:
            if artifact_start is None:
                artifact_start = i
            i += 1

    flush_artifact(n)
    spans.sort(key=lambda s: s.start)
    return spans


def analyze_page(pdf: pikepdf.Pdf, page) -> tuple[PageAnalysis, list]:
    """Parse + segment a page's content stream. Returns (analysis, instructions)."""
    instructions = pikepdf.parse_content_stream(page)
    spans = _segment(instructions)

    resources = page.obj.get("/Resources", Dictionary())
    xobjects = resources.get("/XObject", Dictionary()) if resources else Dictionary()

    image_names = []
    size_counter: Counter = Counter()
    has_any_text = False

    for s in spans:
        if s.kind == "text":
            has_any_text = True
            if s.font_size:
                # Weight by character count (not span count) so a handful of
                # short, large-font headings don't outweigh the much larger
                # amount of normal-size body text when picking the body size.
                char_count = sum(
                    len(str(op)) for instr in instructions[s.start:s.end]
                    if str(instr.operator) in TEXT_SHOW_OPS
                    for op in instr.operands
                    if isinstance(op, (pikepdf.String, str))
                )
                size_counter[round(s.font_size, 1)] += max(char_count, 1)
        elif s.kind == "image":
            name = s.xobject_name
            try:
                xobj = xobjects.get(Name(name)) if name else None
            except Exception:
                xobj = None
            if xobj is not None and str(xobj.get("/Subtype", "")) == "/Image":
                image_names.append(name)
                s.xobject = xobj
            else:
                s.kind = "artifact"  # Form XObject or unresolved: treat conservatively

    body_size = None
    if size_counter:
        max_weight = max(size_counter.values())
        body_size = min(sz for sz, w in size_counter.items() if w == max_weight)
    heading_sizes = sorted({sz for sz in size_counter if body_size and sz > body_size}, reverse=True)

    analysis = PageAnalysis(
        spans=spans, body_size=body_size, heading_sizes=heading_sizes,
        image_names=image_names, has_any_text=has_any_text,
    )
    return analysis, instructions


def classify_tag(font_size: float | None, analysis: PageAnalysis) -> str:
    if font_size and analysis.heading_sizes:
        for level, sz in enumerate(analysis.heading_sizes):
            if font_size >= sz:
                return HEADING_TAGS[min(level, len(HEADING_TAGS) - 1)]
    return "/P"


def build_tagged_stream(pdf: pikepdf.Pdf, page, analysis: PageAnalysis, instructions,
                         describe_image) -> tuple[bytes, list, list]:
    """Rewrite the content stream with BDC/EMC marks.

    describe_image(xobj) -> (alt_text_or_None, needs_review_bool). A None
    alt_text means the image is judged decorative and gets tagged as an
    artifact instead of a Figure.

    Returns (new_stream_bytes, struct_kids, alt_text_flags) where struct_kids
    is a list of dicts: {"tag": "/H1", "mcid": 0, "alt": Optional[str]} in
    document (reading) order, one per real structure element created, and
    alt_text_flags is a list of bools (needs_review) parallel to the Figure
    elements only.
    """
    out = []
    struct_kids = []
    alt_text_flags = []
    mcid = 0

    for s in analysis.spans:
        chunk = instructions[s.start:s.end]
        if s.kind == "artifact":
            out.append(pikepdf.ContentStreamInstruction([Name("/Artifact")], pikepdf.Operator("BMC")))
            out.extend(chunk)
            out.append(pikepdf.ContentStreamInstruction([], pikepdf.Operator("EMC")))
        elif s.kind == "text":
            tag = classify_tag(s.font_size, analysis)
            props = Dictionary({"/MCID": mcid})
            out.append(pikepdf.ContentStreamInstruction([Name(tag), props], pikepdf.Operator("BDC")))
            out.extend(chunk)
            out.append(pikepdf.ContentStreamInstruction([], pikepdf.Operator("EMC")))
            struct_kids.append({"tag": tag, "mcid": mcid, "alt": None})
            mcid += 1
        elif s.kind == "image":
            alt, needs_review = describe_image(s.xobject) if describe_image else (None, False)
            if alt is None:
                # Decorative: no structure element needed, just keep it out
                # of the reading order.
                out.append(pikepdf.ContentStreamInstruction([Name("/Artifact")], pikepdf.Operator("BMC")))
                out.extend(chunk)
                out.append(pikepdf.ContentStreamInstruction([], pikepdf.Operator("EMC")))
                continue
            props = Dictionary({"/MCID": mcid})
            out.append(pikepdf.ContentStreamInstruction([Name("/Figure"), props], pikepdf.Operator("BDC")))
            out.extend(chunk)
            out.append(pikepdf.ContentStreamInstruction([], pikepdf.Operator("EMC")))
            struct_kids.append({"tag": "/Figure", "mcid": mcid, "alt": alt})
            alt_text_flags.append(needs_review)
            mcid += 1

    new_bytes = pikepdf.unparse_content_stream(out)
    return new_bytes, struct_kids, alt_text_flags
