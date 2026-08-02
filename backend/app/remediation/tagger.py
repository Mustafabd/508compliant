"""Builds a PDF/UA-style structure tree (StructTreeRoot + ParentTree) across
a whole document, tagging every page's content via `content.py` and wiring
the resulting marked-content sequences into real structure elements.

This is a best-effort automated tagger, not a substitute for a manual
accessibility audit: heading levels are inferred from font size, reading
order follows content-stream order (not verified against visual layout),
and Form XObjects are conservatively marked as artifacts rather than
recursed into. All of this is surfaced in the accessibility report so
authors know what still needs human review.
"""
from __future__ import annotations

from collections import Counter

import pikepdf
from pikepdf import Array, Dictionary, Name

from . import content


class TaggingStats:
    def __init__(self):
        self.heading_counts: Counter = Counter()
        self.paragraph_count = 0
        self.figure_count = 0
        self.figures_needing_review = 0
        self.decorative_images = 0
        self.pages_with_no_text = []  # 1-indexed page numbers, likely scanned
        self.pages_tagged = 0
        self.form_xobjects_seen = 0


def tag_pdf(pdf: pikepdf.Pdf, alt_text_generator) -> TaggingStats:
    stats = TaggingStats()

    struct_tree_root = pdf.make_indirect(Dictionary(Type=Name.StructTreeRoot))
    doc_elem = pdf.make_indirect(
        Dictionary(Type=Name.StructElem, S=Name("/Document"), P=struct_tree_root, K=Array([]))
    )
    struct_tree_root.K = Array([doc_elem])
    doc_kids = []
    nums = []

    for i, page in enumerate(pdf.pages):
        analysis, instructions = content.analyze_page(pdf, page)

        if not analysis.has_any_text:
            if analysis.image_names:
                stats.pages_with_no_text.append(i + 1)
            # else: genuinely blank page, nothing to flag

        def describe(xobj):
            alt, needs_review = alt_text_generator.describe(xobj)
            if alt is None:
                stats.decorative_images += 1
            else:
                stats.figure_count += 1
                if needs_review:
                    stats.figures_needing_review += 1
            return alt, needs_review

        new_bytes, struct_kids, _ = content.build_tagged_stream(
            pdf, page, analysis, instructions, describe
        )

        if struct_kids:
            page_ref = page.obj
            page_container = pdf.make_indirect(
                Dictionary(Type=Name.StructElem, S=Name("/Sect"), P=doc_elem, Pg=page_ref, K=Array([]))
            )
            kid_refs = []
            mcid_array = [None] * len(struct_kids)
            for k in struct_kids:
                tag_name = k["tag"].lstrip("/")
                elem_dict = Dictionary(
                    Type=Name.StructElem, S=Name("/" + tag_name), P=page_container, Pg=page_ref, K=k["mcid"]
                )
                if k["alt"] is not None:
                    elem_dict["/Alt"] = k["alt"]
                elem = pdf.make_indirect(elem_dict)
                kid_refs.append(elem)
                mcid_array[k["mcid"]] = elem

                if tag_name.startswith("H") and tag_name[1:].isdigit():
                    stats.heading_counts[tag_name] += 1
                elif tag_name == "P":
                    stats.paragraph_count += 1

            page_container.K = Array(kid_refs)
            doc_kids.append(page_container)

            page.obj.StructParents = i
            parent_array = pdf.make_indirect(Array(mcid_array))
            nums.extend([i, parent_array])
            stats.pages_tagged += 1

        page.obj.Contents = pdf.make_stream(new_bytes)

    doc_elem.K = Array(doc_kids)
    struct_tree_root.ParentTree = Dictionary(Nums=Array(nums))
    struct_tree_root.ParentTreeNextKey = len(pdf.pages)

    pdf.Root.StructTreeRoot = struct_tree_root
    pdf.Root.MarkInfo = Dictionary(Marked=True)
    if "/ViewerPreferences" in pdf.Root:
        pdf.Root.ViewerPreferences.DisplayDocTitle = True
    else:
        pdf.Root.ViewerPreferences = Dictionary(DisplayDocTitle=True)

    return stats
