# ADR-0024: The scraper reads CMS bulk exports rather than crawling pages

**Status:** Accepted · **Task:** TASK-013

## Context

The nightly job needs Medicare coverage determinations relevant to the
procedures MedAuth sees. The obvious implementation crawls the Medicare Coverage
Database: find the LCDs matching our codes, fetch each one's page, extract the
text.

CMS also regenerates three archives daily at about 02:00 UTC: `ncd.zip` (~1 MB),
`current_lcd.zip` (~32 MB) and `current_article.zip` (~41 MB). Each holds CSV
tables carrying the **full policy body** — `lcd.csv` in `indication`,
`summary_of_evidence` and their neighbours; `ncd_trkg.csv` in `itm_srvc_desc`
and `indctn_lmtn`.

## Decision

**Three archive fetches per run, and no per-document requests.**

Two further decisions follow from what the exports actually contain:

**The code-to-document index lives in the Articles, not the LCDs.** CMS moved
procedure code lists out of LCDs into companion Billing & Coding Articles, so
`lcd_x_hcpc_code` holds codes for only 66 LCDs — all durable medical equipment —
and essentially no physician CPT codes at all. Filtering it directly matches
almost nothing *while looking exactly like a working scraper that found no work
to do*. The join that works is
`article_x_hcpc_code` -> `article_related_documents` -> `lcd_id`.

**The scrape is scoped to a curated code list**, not everything CMS publishes.
CMS publishes 949 current LCDs and 357 NCDs; indexing all of them would spend
embedding time on durable medical equipment and oncology infusion policies a
private orthopedic or dermatology practice will never order against. Filtering
to the codes MedAuth sees resolves to roughly **eighteen LCDs** — few enough to
be a polite nightly job, specific enough to be a real corpus.

## Consequences

- Crawling would be slower, ruder, and no more complete. The rendered pages add
  AMA and AHA licence boilerplate and navigation chrome, and are not byte-stable
  (per-request CSP nonce) — see ADR-0021.
- Two absences in the code list are decisions, not gaps. `29881` (knee
  arthroscopy with meniscectomy) has **no CMS coverage document at all** — no
  LCD, no billing article — and neither do the dermatology biologics (Cosentyx,
  Taltz, Skyrizi, Stelara, Dupixent). They are missing from Medicare's database
  because they are commercial and pharmacy-benefit territory. Widening the
  filter hunting for them is wasted work; TASK-014 covers them against Aetna
  and BCBS.
- `72148` is MRI of the **lumbar spine**, not a knee MRI, whatever an earlier
  draft of `TASKS.md` called it. Knee MRI without contrast is `73721`. Both are
  in the list.
- The scraper **never chunks, embeds or writes Qdrant itself.** It fetches,
  filters, resolves jurisdictions and uploads to `/policies/ingest`. There is
  one definition of how a policy gets indexed and it lives in that endpoint.
- It exits non-zero when a run could not complete or any document failed to
  ingest, so Kubernetes marks the job failed rather than reporting a green run
  that quietly indexed nothing.

## References

- `services/policy-scraper/src/policy_scraper/mcd.py`, `selection.py`, `codes.py`, `scrape.py`
