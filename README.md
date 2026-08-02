# 508Compliant — Automated PDF Accessibility Converter

Upload a PDF and get back a version with a tagged structure tree, heading
levels, image alt text, a declared title/language, and an accessibility
report listing what was fixed automatically and what still needs a human
look.

## What it actually does

This is a genuine best-effort automated remediation tool, not a rubber
stamp. It:

- Parses each page's content stream and classifies runs of text as
  headings (H1-H6, by relative font size) or paragraphs, and images as
  figures — then rewrites the content stream with proper `BDC/EMC`
  marked-content tags and builds a real PDF `StructTreeRoot` / `ParentTree`
  (not just a "tagged" flag with nothing behind it).
- Marks non-text/decorative content (rules, backgrounds, spacer images) as
  PDF `/Artifact`s so nothing is left in the reading order that shouldn't
  be there.
- Generates image alt text via Claude's vision API if `ANTHROPIC_API_KEY`
  is set; otherwise images are flagged with a placeholder that needs a
  human description.
- Sets the document title (from existing metadata, or guessed from the
  filename) and detects/sets the primary language via `langdetect`.
- Flags pages that look like scanned images with no extractable text —
  it does **not** OCR them; run those through a dedicated OCR tool first.

**What it does not do:** verify color contrast, table headers, form field
labels, meaningful link text, or bookmarks/outline — and heading levels
and reading order are heuristic (font size, and content-stream order),
not a guarantee of semantic correctness. Every conversion returns a report
that says exactly what needs manual review. Treat the output as a strong
head start on 508/WCAG compliance, not a certification.

## Architecture

```
frontend/          static HTML/CSS/JS single-page upload UI
backend/app/
  main.py                 FastAPI app: /api/convert, /api/download/{id}
  remediation/
    content.py            content-stream segmentation + BDC/EMC tagging
    tagger.py              builds the StructTreeRoot/ParentTree across the doc
    alttext.py             image alt-text generation (Claude vision, optional)
    metadata.py             title/language detection + XMP/Info metadata
    report.py               builds the human-facing accessibility checklist
    pipeline.py             orchestrates the above end to end
```

Built on [pikepdf](https://github.com/pikepdf/pikepdf) (qpdf) for all PDF
structure/content manipulation and [pypdf](https://github.com/py-pdf/pypdf)
for text sampling — no system binaries (no Poppler/Ghostscript/Tesseract)
are required to run it.

## Running locally

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Optional: enables real AI-generated alt text for images.
export ANTHROPIC_API_KEY=sk-...

uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Then open http://localhost:8000 — the FastAPI app serves the frontend
directly, so there's nothing else to start.

## Limits

- 25 MB max upload size, 300 pages max per document (configurable in
  `backend/app/main.py` / `backend/app/remediation/pipeline.py`).
- Uploaded and converted files are stored in `backend/jobs/` and deleted
  automatically after 30 minutes.
- Password-protected PDFs are rejected with a clear error; remove the
  password before uploading.
