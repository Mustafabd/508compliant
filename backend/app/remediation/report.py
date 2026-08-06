"""Builds the human-facing accessibility report for a remediated PDF.

Deliberately conservative: anything the pipeline can't verify with high
confidence (reading order on complex layouts, heading hierarchy
correctness, table structure, form field labels, color contrast) is
reported as "needs manual review", never claimed as fixed. Overclaiming
508 compliance is a real legal and usability risk for the document owner.
"""
from __future__ import annotations


MAX_INLINE_FINDINGS = 10


def _format_contrast_finding(f) -> str:
    snippet = f'"{f.text_preview}"'
    if f.status == "fail":
        return f"Page {f.page}: {snippet} — {f.ratio}:1, needs {f.threshold}:1"
    return f"Page {f.page}: {snippet} — {f.reason}"


def build_report(*, filename: str, page_count: int, title: str, title_guessed: bool,
                  language: str, language_confident: bool, stats, scanned_pages: list,
                  contrast_stats=None) -> dict:
    checklist = []

    checklist.append({
        "id": "title",
        "label": "Document title",
        "status": "warning" if title_guessed else "pass",
        "detail": (
            f'Title set to "{title}" (guessed from the filename — please verify it '
            "describes the document)." if title_guessed
            else f'Title "{title}" was already set and preserved.'
        ),
    })

    checklist.append({
        "id": "language",
        "label": "Primary language",
        "status": "pass" if language_confident else "warning",
        "detail": (
            f"Detected and set document language to '{language}'." if language_confident
            else f"Could not confidently detect language from extracted text; defaulted to "
                 f"'{language}'. Verify this is correct."
        ),
    })

    checklist.append({
        "id": "tagged",
        "label": "Tagged PDF / logical structure",
        "status": "pass" if stats.pages_tagged > 0 else "warning",
        "detail": (
            f"Added a tagged structure tree covering {stats.pages_tagged} of {page_count} page(s): "
            f"{sum(stats.heading_counts.values())} heading(s), {stats.paragraph_count} paragraph(s), "
            f"{stats.figure_count} figure(s)."
            if stats.pages_tagged > 0
            else "No taggable text or images were found (the document may be entirely scanned images)."
        ),
    })

    checklist.append({
        "id": "headings",
        "label": "Headings",
        "status": "pass" if stats.heading_counts else "warning",
        "detail": (
            "Heading levels assigned by relative font size: "
            + ", ".join(f"{k} x{v}" for k, v in sorted(stats.heading_counts.items())) + ". "
            "Font-size-based detection can miscategorize atypical layouts — please spot-check heading "
            "levels, especially in multi-column or design-heavy documents."
            if stats.heading_counts
            else "No headings were detected. If this document should have section headings, they may "
                 "need to be added manually."
        ),
    })

    checklist.append({
        "id": "reading_order",
        "label": "Reading order",
        "status": "warning",
        "detail": (
            "Reading order was preserved as the order content appears in the PDF's internal stream. "
            "This matches visual order for most single-column documents, but multi-column layouts, "
            "text boxes, or tables can produce a different visual order — please verify by reading "
            "through the tagged PDF with a screen reader or the tag tree panel in Acrobat."
        ),
    })

    if stats.figure_count == 0 and stats.decorative_images == 0:
        img_status, img_detail = "pass", "No images were found that require alternate text."
    elif stats.figures_needing_review > 0:
        img_status = "warning"
        img_detail = (
            f"{stats.figure_count} image(s) tagged as figures, {stats.figures_needing_review} of which "
            "could not be auto-described (no ANTHROPIC_API_KEY configured, or the description call "
            "failed) and were given a placeholder — replace these with real descriptions before "
            "publishing. "
            f"{stats.decorative_images} image(s) were judged purely decorative and excluded from the "
            "reading order."
        )
    else:
        img_status = "pass"
        img_detail = (
            f"{stats.figure_count} image(s) tagged as figures with generated alt text — please review "
            f"for accuracy. {stats.decorative_images} decorative image(s) were excluded from the reading "
            "order."
        )
    checklist.append({"id": "alt_text", "label": "Image alternate text", "status": img_status, "detail": img_detail})

    if contrast_stats is not None:
        if contrast_stats.checked == 0:
            cs_status = "pass"
            cs_detail = "No text runs with a determinable color were found to check."
        elif contrast_stats.failed > 0:
            cs_status = "fail"
            cs_detail = (
                f"Checked {contrast_stats.checked} text run(s) against WCAG 2.1 AA: "
                f"{contrast_stats.failed} failed, {contrast_stats.needs_review} could not be "
                f"automatically verified, {contrast_stats.passed} passed."
            )
        elif contrast_stats.needs_review > 0:
            cs_status = "warning"
            cs_detail = (
                f"Checked {contrast_stats.checked} text run(s) against WCAG 2.1 AA: no confirmed "
                f"failures, but {contrast_stats.needs_review} could not be automatically verified "
                f"(text over an image/gradient/pattern, or a spot-color fill) and need a manual look."
            )
        else:
            cs_status = "pass"
            cs_detail = f"Checked {contrast_stats.checked} text run(s) against WCAG 2.1 AA — all passed."

        fails = [f for f in contrast_stats.findings if f.status == "fail"]
        reviews = [f for f in contrast_stats.findings if f.status == "needs_review"]
        lines = [cs_detail]
        shown = fails[:MAX_INLINE_FINDINGS]
        if shown:
            lines.append("")
            lines.extend(_format_contrast_finding(f) for f in shown)
            if len(fails) > len(shown):
                lines.append(f"... and {len(fails) - len(shown)} more failure(s).")
        shown_reviews = reviews[:MAX_INLINE_FINDINGS]
        if shown_reviews:
            lines.append("")
            lines.append("Needs manual review:")
            lines.extend(_format_contrast_finding(f) for f in shown_reviews)
            if len(reviews) > len(shown_reviews):
                lines.append(f"... and {len(reviews) - len(shown_reviews)} more.")

        checklist.append({
            "id": "color_contrast",
            "label": "Color contrast (WCAG AA)",
            "status": cs_status,
            "detail": "\n".join(lines),
        })

    if scanned_pages:
        checklist.append({
            "id": "scanned",
            "label": "Scanned / image-only pages",
            "status": "fail",
            "detail": (
                f"Page(s) {', '.join(str(p) for p in scanned_pages)} appear to contain only images with "
                "no extractable text (likely scanned). This tool does not perform OCR — run these pages "
                "through an OCR tool (e.g. Adobe Acrobat's 'Recognize Text' or ocrmypdf) before "
                "publishing, or they will be completely inaccessible to screen readers."
            ),
        })

    checklist.append({
        "id": "manual_review",
        "label": "Not automatically checked",
        "status": "warning",
        "detail": (
            "This tool does not check table row/column headers, form field labels, link text "
            "meaningfulness, or document outline (bookmarks). Review these manually against "
            "WCAG 2.1 AA / Section 508 before publishing."
        ),
    })

    result = {
        "filename": filename,
        "page_count": page_count,
        "title": title,
        "language": language,
        "checklist": checklist,
    }
    if contrast_stats is not None:
        result["contrast_findings"] = [
            {
                "page": f.page,
                "status": f.status,
                "ratio": f.ratio,
                "threshold": f.threshold,
                "text_preview": f.text_preview,
                "font_size": f.font_size,
                "is_bold": f.is_bold,
                "reason": f.reason,
            }
            for f in contrast_stats.findings
        ]
    return result
