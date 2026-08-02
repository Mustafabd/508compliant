"""Document-level metadata remediation: title and language.

WCAG/508 requires every PDF to have a meaningful title (shown in place of
the filename) and a declared primary language so assistive tech picks the
right voice/braille table.
"""
from __future__ import annotations

import re

import pikepdf
from langdetect import DetectorFactory, LangDetectException, detect

DetectorFactory.seed = 0  # deterministic detection


def existing_title(pdf: pikepdf.Pdf) -> str | None:
    try:
        title = pdf.docinfo.get("/Title")
    except Exception:
        title = None
    if title:
        text = str(title).strip()
        if text:
            return text
    return None


def guess_title(pdf: pikepdf.Pdf, filename: str) -> tuple[str, bool]:
    """Returns (title, was_guessed)."""
    existing = existing_title(pdf)
    if existing:
        return existing, False

    stem = re.sub(r"\.pdf$", "", filename, flags=re.IGNORECASE)
    stem = re.sub(r"[_\-]+", " ", stem).strip()
    guessed = stem.title() if stem else "Untitled Document"
    return guessed, True


def detect_language(sample_text: str) -> tuple[str, bool]:
    """Returns (bcp47_language_tag, was_confident). Defaults to en-US."""
    text = (sample_text or "").strip()
    if len(text) < 20:
        return "en-US", False
    try:
        code = detect(text)
    except LangDetectException:
        return "en-US", False
    lang_map = {"en": "en-US"}
    return lang_map.get(code, code), True


def apply_metadata(pdf: pikepdf.Pdf, title: str, lang: str) -> None:
    with pdf.open_metadata() as meta:
        meta["dc:title"] = title
        meta["dc:language"] = lang
    pdf.docinfo["/Title"] = title
    pdf.Root["/Lang"] = lang
