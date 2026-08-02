"""Alt-text generation for images found in a PDF.

If ANTHROPIC_API_KEY is set in the environment, each image is sent to
Claude's vision API for a real description. Otherwise (or if a call
fails) a placeholder is used and the image is flagged in the report as
needing a human-written description -- we never fabricate a confident
description we can't back up.
"""
from __future__ import annotations

import base64
import io
import os

import httpx
import pikepdf

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_ALT_TEXT_MODEL", "claude-sonnet-5")
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"

MIN_DECORATIVE_DIMENSION = 8  # px; smaller than this is almost certainly a spacer/rule

PLACEHOLDER_ALT = "Image (description needed)"

ALT_TEXT_PROMPT = (
    "This image was extracted from a PDF document. Write a concise, factual "
    "alt-text description (max 150 characters) suitable for a screen reader. "
    "Describe what the image shows; do not start with 'image of' or similar. "
    "If the image is purely decorative (a line, spacer, background texture, "
    "or bullet glyph) respond with exactly: DECORATIVE"
)


class AltTextGenerator:
    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or ANTHROPIC_API_KEY
        self.calls_made = 0
        self.calls_failed = 0

    def describe(self, xobj) -> tuple[str | None, bool]:
        """Returns (alt_text, needs_review).

        alt_text is None only for images judged purely decorative (which get
        tagged as artifacts instead of Figures by the caller). needs_review
        is True when the description is a heuristic placeholder, not a real
        one, and should be flagged to the user.
        """
        try:
            pdf_image = pikepdf.PdfImage(xobj)
            width, height = pdf_image.width, pdf_image.height
        except Exception:
            return PLACEHOLDER_ALT, True

        if width < MIN_DECORATIVE_DIMENSION or height < MIN_DECORATIVE_DIMENSION:
            return None, False

        if not self.api_key:
            return PLACEHOLDER_ALT, True

        try:
            pil_image = pdf_image.as_pil_image()
            buf = io.BytesIO()
            pil_image.convert("RGB").save(buf, format="JPEG", quality=85)
            b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        except Exception:
            return PLACEHOLDER_ALT, True

        try:
            resp = httpx.post(
                ANTHROPIC_API_URL,
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": ANTHROPIC_MODEL,
                    "max_tokens": 100,
                    "messages": [{
                        "role": "user",
                        "content": [
                            {"type": "image", "source": {
                                "type": "base64", "media_type": "image/jpeg", "data": b64,
                            }},
                            {"type": "text", "text": ALT_TEXT_PROMPT},
                        ],
                    }],
                },
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            text = "".join(
                block.get("text", "") for block in data.get("content", [])
                if block.get("type") == "text"
            ).strip()
            self.calls_made += 1
            if not text:
                return PLACEHOLDER_ALT, True
            if text.strip().upper() == "DECORATIVE":
                return None, False
            return text[:300], False
        except Exception:
            self.calls_failed += 1
            return PLACEHOLDER_ALT, True
