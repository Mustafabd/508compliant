"""Top-level orchestration: input PDF path -> remediated PDF + report."""
from __future__ import annotations

import pikepdf
from pypdf import PdfReader

from . import alttext, contrast, metadata, report, tagger

MAX_PAGES = 300


class RemediationError(Exception):
    """Raised for problems the caller should show to the user directly."""


def _sample_text(input_path: str, max_pages: int = 3) -> str:
    try:
        reader = PdfReader(input_path)
        chunks = []
        for page in reader.pages[:max_pages]:
            chunks.append(page.extract_text() or "")
        return "\n".join(chunks)
    except Exception:
        return ""


def run(input_path: str, output_path: str, filename: str) -> dict:
    try:
        pdf = pikepdf.Pdf.open(input_path)
    except pikepdf.PasswordError as exc:
        raise RemediationError("This PDF is password-protected. Remove the password and try again.") from exc
    except pikepdf.PdfError as exc:
        raise RemediationError(
            "Could not open this file as a PDF. It may be corrupted or not a valid PDF."
        ) from exc

    try:
        page_count = len(pdf.pages)
        if page_count == 0:
            raise RemediationError("This PDF has no pages.")
        if page_count > MAX_PAGES:
            raise RemediationError(
                f"This PDF has {page_count} pages; the free converter supports up to {MAX_PAGES}."
            )

        alt_gen = alttext.AltTextGenerator()
        stats = tagger.tag_pdf(pdf, alt_gen)
        contrast_stats = contrast.check_pdf(pdf)

        sample_text = _sample_text(input_path)
        title, title_guessed = metadata.guess_title(pdf, filename)
        lang, lang_confident = metadata.detect_language(sample_text)
        metadata.apply_metadata(pdf, title, lang)

        pdf.save(output_path)
    finally:
        pdf.close()

    return report.build_report(
        filename=filename,
        page_count=page_count,
        title=title,
        title_guessed=title_guessed,
        language=lang,
        language_confident=lang_confident,
        stats=stats,
        scanned_pages=stats.pages_with_no_text,
        contrast_stats=contrast_stats,
    )
