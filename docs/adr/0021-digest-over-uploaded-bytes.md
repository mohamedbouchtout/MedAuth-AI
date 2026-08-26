# ADR-0021: The digest is over the uploaded bytes, and HTML is a first-class format

**Status:** Accepted · **Task:** TASK-011, TASK-013

## Context

Two questions, decided together because the answer to one forced the other.

**What gets hashed?** `content_hash` decides whether a nightly scrape re-embeds
a document. Hashing the extracted *text* would make two source files with
identical text into one document.

**What formats are ingested?** Commercial payers publish policy PDFs. CMS does
not: its Medicare Coverage Database publishes LCDs and NCDs as HTML — the "PDF"
affordance on the site is the browser's own print-to-PDF — and the bulk export
carries the document body as HTML fragments inside CSV fields.

The tempting simplification was to render that HTML to a PDF so the pipeline
could stay PDF-only. **That was rejected on measurement:** PyMuPDF's output is
not byte-deterministic, so the same document rendered on two nights produces two
digests, every nightly scrape reads as an update, and the entire corpus is
re-embedded daily — exactly the cost `content_hash` exists to avoid.

Crawling the rendered CMS pages instead was rejected for the same class of
reason: each response carries a per-request CSP nonce, so a digest taken over one
changes on every fetch.

## Decision

- **The digest is SHA-256 over the raw uploaded bytes**, never over extracted
  text. Two documents whose text happens to match but whose bytes differ are
  distinct source files for audit purposes.
- **`application/pdf` and `text/html` are both first-class.**
  `documents.extract_text()` dispatches on the declared content type to
  `pdf.py` (PyMuPDF) or `markup.py`.

`markup.py` uses stdlib `html.parser` rather than a parsing library: the job is
to get prose out of `<p>`, `<ul>` and `<table>` markup with structure turned into
blank lines, and BeautifulSoup would be a dependency the work does not need. It
is built to read **fragments** — a policy's "Coverage Indications, Limitations,
and/or Medical Necessity" section — not well-formed pages, because fragments are
what actually arrive.

PyMuPDF is imported as `pymupdf` rather than the legacy `fitz` alias: only the
`pymupdf` module ships a `py.typed` marker, so importing it keeps the module
inside `mypy --strict` instead of needing an `ignore_missing_imports` override.

## Consequences

- A nightly scrape re-embeds only what CMS actually changed. In practice most
  documents are skipped.
- The scraper concatenates a policy's HTML fragments in a **fixed order**, which
  is a module constant rather than dictionary iteration, because the digest is
  taken over exactly those bytes. A change in field order would look like every
  policy changing at once.
- Export metadata (`last_updated`, `lcd_version`) is deliberately **not** part
  of the document: it describes the export, not the policy, and folding it in
  would re-ingest documents whose text never moved.

## References

- `services/track-b-rag/src/track_b_rag/documents.py`, `pdf.py`, `markup.py`
- `services/policy-scraper/src/policy_scraper/documents.py`
