# ADR-0021: The digest is over the uploaded bytes, and HTML is a first-class format

**Status:** Accepted, amended by TASK-009 · **Task:** TASK-011, TASK-013, TASK-009

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

- **The digest is SHA-256 over the uploaded bytes**, never over extracted
  text. Two documents whose text happens to match but whose bytes differ are
  distinct source files for audit purposes.
- **For `text/html`, every `<script>` and `<style>` element is cut out of those
  bytes first.** *Amended by TASK-009; see "Amendment" below.* PDFs are hashed
  exactly as uploaded.
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

## Amendment (TASK-009): script and style are cut out of HTML before hashing

**Status:** Accepted. This narrows the raw-bytes rule above for HTML. It is a
deliberate change, recorded here so that it does not read as a regression.

### Context

Every run of `scripts/seed-policies.py` reported every Aetna Clinical Policy
Bulletin `updated`, and re-embedded all 4,941 of their chunks (measured, 2,236
of them one document), while every BCBSMA PDF correctly reported `unchanged`. Two fetches of any Aetna page were the same
length and differed on exactly one line: an Akamai mPulse
(`s.go-mpulse.net/boomerang/`) `<script>` that the CDN injects with a fresh
per-request token. This was checked on all nine Aetna documents, and on each
the rest of the page — seven more scripts, a style block, `<noscript>` blocks,
52 comments, inline event handlers — was identical between fetches, as was the
extracted text.

It is the failure the Context above already names for rendered CMS pages (a
per-request CSP nonce), reached by a different route. The reasoning was sound;
it was not carried across to the seed script, which does fetch rendered pages.

### Decision

- **Hash HTML with its `<script>` and `<style>` elements cut out as byte
  ranges.** The rest of the bytes are kept exactly as published, never parsed
  and re-serialised. HTML with neither element therefore digests to the SHA-256
  of its raw bytes, exactly as before, so no digest already stored for
  script-free HTML moves. That covers every CMS document, whose export fragments
  carry neither.
- **Not "hash the extracted text."** The audit reason the original decision gives
  still holds: two HTML files differing anywhere outside a script or style
  element, markup included, remain two documents.
- **One definition, in `packages/html-digest`.** Three places depend on which
  bytes count as script or style: track-b-rag's `content_digest` (authoritative),
  policy-scraper's `content_hash` (its pre-upload skip), and `markup.py`'s text
  extraction. All three call the one scanner. If the two digests drifted apart,
  nothing would fail: the scraper would upload every document every night and be
  told `unchanged` each time. `tests/unit/test_service_agreement.py` calls both
  services' own functions and asserts they agree, and
  `detect-changed-members.sh` re-runs it when either service changes.
- **The scanner is written for this package rather than taken from
  `html.parser`.** The stdlib tokenizer's handling of script content has changed
  between patch releases (CI runs 3.12, development 3.13), and a digest must not
  move because the interpreter did. It scans bytes, which is exact for UTF-8 and
  cp1252 because every character it matches on is ASCII.

### What the digest deliberately still covers

`<noscript>`, attributes (inline event handlers included) and comments. None of
them varies between fetches of any document in the corpus today, and stripping
them would be a broader claim than the evidence supports. A rotating token
injected into one of them would defeat the digest again. That fails in the
wasteful direction, a re-embed, and never by hiding a revision, because what is
stripped is a subset of what text extraction already discards. The nightly
`commercial-payer-policies` job fetches each Aetna page twice and fails if the
two digests differ.

### Consequences

- A second fetch of an unchanged Aetna page ingests as `unchanged`.
- **Each Aetna document reports `updated` once more after this lands**, because
  its stored digest was taken over the raw bytes. That is the fix taking effect;
  a second consecutive `updated` would be the bug.
- `content_digest` takes the declared content type; the digest is no longer
  independent of it for HTML that carries script or style.

## References

- `services/track-b-rag/src/track_b_rag/documents.py`, `pdf.py`, `markup.py`
- `services/policy-scraper/src/policy_scraper/documents.py`
- `packages/html-digest/src/html_digest/strip.py` (TASK-009)
