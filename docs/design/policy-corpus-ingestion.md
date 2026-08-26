# Design: Building the Policy Corpus

**Components:** `services/policy-scraper`, `track-b-rag`'s `/policies/ingest`,
`scripts/seed-policies.py` · **Tasks:** TASK-011, TASK-013, TASK-014

The RAG path can only answer from documents it holds. This is how they get there.

## Two sources, one pipeline

| Source | Format | Mechanism | Task |
|---|---|---|---|
| CMS Medicare Coverage Database | HTML fragments in CSV | Nightly `policy-scraper` CronJob | TASK-013 |
| Commercial payers (Aetna, BCBS) | PDF | `scripts/seed-policies.py`, run by hand | TASK-014 |

Both end at the same place: **`POST /policies/ingest`**. Nothing else chunks,
embeds or writes Qdrant. There is one definition of how a policy gets indexed,
and keeping it that way is why the scraper is deliberately thin.

## `POST /policies/ingest`

Takes a document plus metadata (`policy_id`, `payer`, `plan_type`, `state`,
`jurisdiction_states`, `source_url`, `effective_date`, `content_type`) and runs:

1. **Digest** — SHA-256 over the **raw uploaded bytes**, never over extracted
   text. Two documents whose text matches but whose bytes differ are distinct
   source files for audit purposes.
2. **Dedup** on `(policy_id, content_hash)`:

   | State | Action | Reported |
   |---|---|---|
   | No row | Index, insert | `created` |
   | Digest matches | Nothing | `unchanged` |
   | Digest differs | Re-index, update | `updated` |

3. **Extract text** — dispatched on content type to PyMuPDF or the HTML reader.
4. **Chunk** — 800 characters, 150 overlap.
5. **Embed** — `BAAI/bge-large-en-v1.5`, locally.
6. **Write Qdrant**, then **write Postgres**. That order is load-bearing
   ([ADR-0020](../adr/0020-qdrant-written-before-postgres.md)).

Point ids are a deterministic UUID5 of `(policy_id, chunk_index)` under a fixed
namespace, so re-indexing a changed document replaces its points rather than
accumulating duplicates.

**No audit row is written.** Policy documents are public payer publications with
no patient linkage ([ADR-0006](../adr/0006-audit-log-is-phi-only.md)). The INFO
log is the operational record.

## Two document formats, and why

Commercial payers publish PDFs. **CMS does not** — its Medicare Coverage Database
publishes LCDs and NCDs as HTML (the "PDF" affordance on the site is the
browser's own print-to-PDF), and the bulk export carries the document body as
HTML fragments inside CSV fields.

Rendering that HTML to a PDF so the pipeline could stay PDF-only was **rejected
on measurement**: PyMuPDF's output is not byte-deterministic, so the same
document rendered on two nights produces two digests, every nightly scrape reads
as an update, and the entire corpus is re-embedded daily — exactly the cost
`content_hash` exists to avoid
([ADR-0021](../adr/0021-digest-over-uploaded-bytes.md)).

The HTML reader uses stdlib `html.parser`, not BeautifulSoup: the job is to get
prose out of `<p>`, `<ul>` and `<table>` markup with structure turned into blank
lines. It is built to read **fragments** — a policy's "Coverage Indications,
Limitations, and/or Medical Necessity" section — because fragments are what
actually arrive, and anything requiring a well-formed page would reject the real
input.

## The nightly scrape

`python -m policy_scraper`, a one-shot Kubernetes CronJob. A run:

1. **Fetch three archives** from CMS — `ncd.zip` (~1 MB), `current_lcd.zip`
   (~32 MB), `current_article.zip` (~41 MB), regenerated daily around 02:00 UTC.
   **That is every request the service makes.** The exports carry the full policy
   body, so there is nothing to crawl.
2. **Select** which LCDs matter and resolve each one's jurisdiction to USPS state
   codes.
3. **Assemble** each document from the export's fields, in a fixed order, and
   digest it.
4. **Skip** whatever the database already holds under that digest, and upload the
   rest.

The run summary is the operational record. The process **exits non-zero** when a
run could not complete or any document failed to ingest, so Kubernetes marks the
job failed rather than reporting a green run that quietly indexed nothing.

### Two non-obvious joins

**The code-to-document index lives in the Articles, not the LCDs.** CMS moved
procedure code lists out of LCDs into companion Billing & Coding Articles, so
`lcd_x_hcpc_code` holds codes for only 66 LCDs — all durable medical equipment —
and essentially no physician CPT codes. Filtering it directly matches almost
nothing *while looking exactly like a working scraper that found no work to do*.
The join that works is
`article_x_hcpc_code` → `article_related_documents` → `lcd_id`.

**An LCD's jurisdiction is a set of states, not one state.** It is issued by a
Medicare Administrative Contractor and applies across that contractor's whole
jurisdiction — a median of **12 states**, up to 48. Resolving it is
`lcd_x_contractor` → `contractor_jurisdiction` → `state_lookup`, and then
normalising, because CMS's state vocabulary is not USPS: `DN`/`QN`/`UN` for parts
of New York, `NF`/`SF` for California, `EM`/`WM` for Missouri, and a
four-character `CNMI` that does not fit `CHAR(2)` at all
([ADR-0023](../adr/0023-usps-jurisdictions-multi-state-policies.md)).

A multi-state policy is stored as **one document with a list of states**, never
one copy per state — copying would duplicate identical text a median of 12 times
in Qdrant, at 12x the embedding cost, with near-duplicate chunks crowding each
other out of `TOP_K = 8`.

### Document assembly is byte-sensitive

A policy is spread across several CSV columns, each holding an HTML fragment. The
document uploaded is those fragments concatenated **in a fixed order**, which is
a module constant rather than dictionary iteration, because the digest is taken
over exactly those bytes — a change in field order would look like every policy
changing at once.

For the same reason the row's own metadata (`last_updated`, `lcd_version`) is
deliberately **not** part of the document: it describes the export, not the
policy, and folding it in would re-ingest documents whose text never moved.

### Scope

CMS publishes 949 current LCDs and 357 NCDs. Indexing all of them would spend
embedding time on durable medical equipment and oncology infusion policies a
private orthopedic or dermatology practice will never order against. Filtering to
a curated code list resolves to roughly **18 LCDs** — few enough to be a polite
nightly job, specific enough to be a real corpus
([ADR-0024](../adr/0024-scraper-reads-bulk-exports.md)).

Two facts about that list that are easy to get wrong, and were:

- **`72148` is MRI of the lumbar spine**, not a knee MRI, whatever an earlier
  draft of `TASKS.md` called it. Knee MRI without contrast is `73721`. Both are
  in the list.
- **`29881` (knee arthroscopy with meniscectomy) is deliberately absent.** It has
  no CMS coverage document at all — no LCD, no billing article — and neither do
  the dermatology biologics (Cosentyx, Taltz, Skyrizi, Stelara, Dupixent). They
  are not missing from the filter; they are missing from Medicare's database,
  because they are commercial and pharmacy-benefit territory. TASK-014 covers
  them against Aetna and BCBS.

### Politeness

Three courtesies, all requirements rather than nice-to-haves:

- **A User-Agent naming this scraper and a contact address.** Not decoration:
  `www.cms.gov` answers 403 to some clients purely on their User-Agent.
- **robots.txt honoured per host**, fetched once and remembered for the run. The
  database UI is `www.cms.gov` and the exports are on `downloads.cms.gov` —
  separate hosts, separate files. A host serving no robots.txt permits
  everything, which is `downloads.cms.gov`.
- **A delay between requests.** CMS sets no `Crawl-delay`, so this is our own
  policy. There are only three requests in a run.

`urllib.robotparser` is **not** used: its matching is a prefix comparison with no
wildcard handling, so `Disallow: /*?` is treated as a literal prefix and matches
nothing. Against CMS's file that reaches the right answer for the wrong reason,
which is worse than being wrong. The matcher here implements the two rules the
standard specifies — `*` and `$` wildcards, and longest-match-wins with `Allow`
winning a tie ([ADR-0025](../adr/0025-own-robots-txt-matcher.md)).

### The `store` read is only an optimisation

Checking what has already been ingested saves sending a document CMS has not
touched since last night — with a few dozen documents a run, that is most of
them. But `/policies/ingest` computes the digest from the bytes it receives and
decides `created`/`unchanged`/`updated` for itself, and **that** decision is
authoritative. Losing a race costs one redundant upload and never a wrong answer,
so nothing here needs a lock.

## Payer identity at ingestion

Documents are ingested under a **canonical payer slug**, never a display name.
The payer's own spelling is kept in the Postgres row for humans to read; slugs
are for matching ([ADR-0022](../adr/0022-canonical-payer-slugs.md)).

**A payer family is not one payer.** Seed and ingest under the publishing
licensee's slug — `bcbs-ma`, not the generic `blue-cross-blue-shield` bucket.
Collapsing them would let one BCBS licensee's policy answer a query about
another: a wrong answer served silently, strictly worse than an empty retrieval
plus a WARNING.

## Validation against the real source

`tests/integration/test_cms_live.py` runs against the actual CMS export behind an
environment gate, and the nightly workflow turns that gate on
([ADR-0038](../adr/0038-gated-live-tests-need-a-schedule.md)). Fixtures captured
from real responses back the unit suite, so the parser is validated against what
CMS actually publishes rather than against what we imagined it publishes.
