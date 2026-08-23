"""Seed the local Qdrant with real commercial payer policies for development.

    TRACK_B_RAG_URL=http://localhost:8002 uv run python scripts/seed-policies.py

Start the stack first (``docker compose up``) and have ``track-b-rag`` running —
this script uploads documents, it does not index them. Everything expensive
(chunking, embedding, the Qdrant write, the created/updated/unchanged decision)
belongs to ``POST /policies/ingest``, exactly as it does for the CMS scraper.
Re-running is safe: ingest's own ``content_hash`` dedup makes a second run a
no-op for every document whose text has not changed.

SCOPE: AETNA AND BCBS ONLY
--------------------------
Medicare coverage is *not* seeded here. CMS publishes no policy PDFs at all —
the Medicare Coverage Database's "PDF" affordance is the browser's own
print-to-PDF, and the real documents are HTML fragments inside CSV exports — so
``services/policy-scraper`` (TASK-013) owns that path end to end. Seed Medicare
with::

    uv run python -m policy_scraper

Running both against the same corpus would fight over the same ``policy_id``
values (``cms-lcd-L39529``), each overwriting the other's Qdrant points.

TWO CONTENT TYPES, BECAUSE THE PAYERS DIFFER
--------------------------------------------
Aetna publishes Clinical Policy Bulletins as HTML pages; Blue Cross Blue Shield
of Massachusetts publishes medical policies as PDFs. Both go through the one
ingest endpoint, which dispatches on the declared ``content_type``. Nothing here
converts between them — rendering HTML to PDF would break the content hash, for
the reason recorded in TASK-013.

WHY A CURATED LIST AND NOT A CRAWLER
-------------------------------------
Both payers render their policy indexes in JavaScript, so there is no
machine-readable listing to walk, and walking it would be the impolite way to
find out. Every URL below was read off the rendered index by a human and
verified to fetch. Add entries the same way.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from dataclasses import dataclass
from typing import Final

import httpx

from payer_vocab import is_known_payer, normalize_payer
from policy_scraper.fetch import PoliteClient, RobotsDisallowed

logger: Final = logging.getLogger("seed-policies")

#: Named in outbound requests so the payers can see who is fetching and reach a
#: human. Some healthcare sites answer 403 on User-Agent alone, so this is
#: load-bearing rather than decoration.
USER_AGENT: Final = "MedAuthAI-PolicyScraper/1.0 (+mohamedbouchtout@gmail.com)"

#: Our own politeness policy. Neither payer publishes a Crawl-delay, and this
#: script makes well under twenty requests, so the delay costs almost nothing.
DELAY_SECONDS: Final = 1.5
TIMEOUT_SECONDS: Final = 60.0

DEFAULT_TRACK_B_RAG_URL: Final = "http://localhost:8002"


@dataclass(frozen=True)
class SeedPolicy:
    """One document to fetch and hand to ``/policies/ingest``.

    ``payer`` is the payer's own spelling. It is normalised to a canonical slug
    before it is sent, because that slug is what the Qdrant filter matches and
    what the ``rag:`` cache key interpolates — see CLAUDE.md, "Payer and
    jurisdiction identity". The display spelling is kept here so a human reading
    this list sees the payer's name rather than a slug.
    """

    policy_id: str
    payer: str
    title: str
    source_url: str
    content_type: str
    state: str | None = None

    @property
    def filename(self) -> str:
        """A filename for the multipart part, derived from the declared type."""
        suffix = "pdf" if self.content_type == "application/pdf" else "html"
        return f"{self.policy_id}.{suffix}"


#: The dev corpus. One entry per document, each with a comment saying which
#: target code it is here for — the same shape as TASK-013's CPT/HCPCS filter,
#: and for the same reason: a list nobody can explain stops being curated and
#: starts being accumulated.
#:
#: ``state`` is Massachusetts for the BCBSMA documents because that licensee's
#: policies apply there and nowhere else. The Aetna CPBs are national and carry
#: no state, which is what lets them match every query through the retrieval
#: filter's ``IsNullCondition``.
SEED_POLICIES: Final[tuple[SeedPolicy, ...]] = (
    # --- Blue Cross Blue Shield of Massachusetts ----------------------------
    # Seeded under the licensee's own slug (``bcbs-ma``), never the generic
    # ``blue-cross-blue-shield`` bucket: Association licensees publish their own
    # criteria, and a shared slug would let one licensee's policy answer for
    # another. See CLAUDE.md, "A payer family is not one payer."
    SeedPolicy(
        # 73721 / 73718 — lower-extremity joint MRI, the orthopedic MRI case.
        # BCBSMA delegates advanced imaging review to Carelon, so the code-level
        # criteria live in the Carelon companion documents rather than in a
        # policy named after the body part.
        policy_id="bcbsma-933-carelon-extremity-imaging",
        payer="Blue Cross Blue Shield of Massachusetts",
        title="Carelon Extremity Imaging CPT and Diagnoses Codes",
        source_url=(
            "https://www.bluecrossma.org/medical-policies/sites/g/files/csphws2091/"
            "files/acquiadam-assets/933%20Carelon%20Extremity%20Imaging%20CPT%20and%20"
            "Diagnoses%20Codes%20prn.pdf"
        ),
        content_type="application/pdf",
        state="MA",
    ),
    SeedPolicy(
        # 72148 / 72149 — lumbar spine MRI. Same Carelon delegation as above.
        policy_id="bcbsma-935-carelon-spine-imaging",
        payer="Blue Cross Blue Shield of Massachusetts",
        title="Carelon Spine Imaging CPT and Diagnoses Codes",
        source_url=(
            "https://www.bluecrossma.org/medical-policies/sites/g/files/csphws2091/"
            "files/acquiadam-assets/935%20Carelon%20Spine%20Imaging%20CPT%20and%20"
            "Diagnoses%20Codes%20prn.pdf"
        ),
        content_type="application/pdf",
        state="MA",
    ),
    SeedPolicy(
        # The umbrella code list for the whole Carelon advanced-imaging program.
        # Here so a query for an imaging code outside the two body regions above
        # still retrieves something that says who reviews it.
        policy_id="bcbsma-900-carelon-advanced-imaging",
        payer="Blue Cross Blue Shield of Massachusetts",
        title="Carelon Advanced Imaging Radiology CPT and HCPCS Codes",
        source_url=(
            "https://www.bluecrossma.org/medical-policies/sites/g/files/csphws2091/"
            "files/acquiadam-assets/900%20Carelon%20Advanced%20Imaging%20Radiology%20"
            "CPT%20and%20HCPCS%20Codes%20prn.pdf"
        ),
        content_type="application/pdf",
        state="MA",
    ),
    SeedPolicy(
        # Not tied to one CPT code: this is the policy that says a technology is
        # investigational and therefore not covered. A denial-risk answer that
        # cannot cite it is missing the most common commercial denial reason.
        policy_id="bcbsma-400-noncovered-services",
        payer="Blue Cross Blue Shield of Massachusetts",
        title="Medical Technology Assessment Investigational Non Covered Services",
        source_url=(
            "https://www.bluecrossma.org/medical-policies/sites/g/files/csphws2091/"
            "files/acquiadam-assets/400%20Medical%20Technology%20Assessment%20"
            "Investigational%20Non%20Covered%20Services.pdf"
        ),
        content_type="application/pdf",
        state="MA",
    ),
    # --- Aetna Clinical Policy Bulletins ------------------------------------
    # HTML, not PDF. Aetna's CPB index sits behind a terms-acceptance gate and
    # renders its listing from two chained dropdowns (Alpha/Category, then the
    # category), so these numbers were read off the rendered listing by a human.
    # The individual CPB URLs below need no acceptance and fetch directly, which
    # is why this script needs no browser.
    #
    # Every entry was verified by fetching the document and confirming the codes
    # named in its comment appear in the extracted text — the BCBSMA 400 URL in
    # the block above was wrong on first writing precisely because it was taken
    # from an index without being fetched.
    SeedPolicy(
        # 72148 — lumbar spine MRI appears in this CPB's code list, alongside the
        # imaging criteria Aetna applies before spinal surgery.
        policy_id="aetna-cpb-0743-spinal-surgery",
        payer="Aetna",
        title="Spinal Surgery: Laminectomy and Fusion",
        source_url="https://www.aetna.com/cpb/medical/data/700_799/0743.html",
        content_type="text/html",
    ),
    SeedPolicy(
        # 72148 / 72149 — lumbar spine MRI, the dedicated imaging policy.
        # Both codes verified present in the fetched document.
        policy_id="aetna-cpb-0236-spine-mri",
        payer="Aetna",
        title="Magnetic Resonance Imaging (MRI) and Computed Tomography (CT) of the Spine",
        source_url="https://www.aetna.com/cpb/medical/data/200_299/0236.html",
        content_type="text/html",
    ),
    SeedPolicy(
        # 73721 / 73718 — lower-extremity joint MRI, including the knee.
        policy_id="aetna-cpb-0171-extremity-mri",
        payer="Aetna",
        title="Magnetic Resonance Imaging (MRI) of the Extremities",
        source_url="https://www.aetna.com/cpb/medical/data/100_199/0171.html",
        content_type="text/html",
    ),
    SeedPolicy(
        # 29881 — knee arthroscopy with meniscectomy. This is the code CMS
        # has no coverage document for at all, which is exactly why the
        # commercial corpus has to carry it. See TASK-013's scope note.
        policy_id="aetna-cpb-0673-knee-arthroscopy",
        payer="Aetna",
        title="Knee Arthroscopy / Osteoarthritis of the Knee",
        source_url="https://www.aetna.com/cpb/medical/data/600_699/0673.html",
        content_type="text/html",
    ),
    SeedPolicy(
        # J7321 / J7325 hyaluronan knee injections, and 20610 major joint
        # injection. Also absent from Medicare's database.
        policy_id="aetna-cpb-0179-viscosupplementation",
        payer="Aetna",
        title="Viscosupplementation",
        source_url="https://www.aetna.com/cpb/medical/data/100_199/0179.html",
        content_type="text/html",
    ),
    SeedPolicy(
        # 64483 / 62323 — epidural steroid injections. Chosen over CPB 0934
        # (Epidural Injection Technologies), which carries 62323 but not
        # 64483; this one carries both, verified in the fetched text.
        policy_id="aetna-cpb-0016-back-pain-invasive",
        payer="Aetna",
        title="Back Pain - Invasive Procedures",
        source_url="https://www.aetna.com/cpb/medical/data/1_99/0016.html",
        content_type="text/html",
    ),
    SeedPolicy(
        # Dermatology biologic. Provider-administered biologics sit on the
        # medical benefit and so appear in the medical CPB index.
        policy_id="aetna-cpb-0905-secukinumab",
        payer="Aetna",
        title="Secukinumab (Cosentyx)",
        source_url="https://www.aetna.com/cpb/medical/data/900_999/0905.html",
        content_type="text/html",
    ),
    SeedPolicy(
        # Dermatology biologic, as above.
        policy_id="aetna-cpb-1009-risankizumab",
        payer="Aetna",
        title="Risankizumab-rzaa (Skyrizi)",
        source_url="https://www.aetna.com/cpb/medical/data/1000_1099/1009.html",
        content_type="text/html",
    ),
    SeedPolicy(
        # Dermatology biologic, as above.
        #
        # Taltz (ixekizumab) and Dupixent (dupilumab) are deliberately absent:
        # they are self-administered and sit on the pharmacy benefit, so they
        # are published in Aetna's separate Pharmacy CPB index rather than
        # this one. Adding them means seeding from that index, not widening
        # the search here.
        policy_id="aetna-cpb-0912-ustekinumab",
        payer="Aetna",
        title="Ustekinumab (Stelara)",
        source_url="https://www.aetna.com/cpb/medical/data/900_999/0912.html",
        content_type="text/html",
    ),
)


class SeedFailed(RuntimeError):
    """Raised when a document could not be fetched or the endpoint rejected it."""


def _form_fields(policy: SeedPolicy) -> dict[str, str]:
    """Return the multipart metadata fields for one document.

    The payer is normalised here rather than at the call site so that every
    document in this corpus is indexed under the same slug the query path will
    build from a FHIR ``Coverage`` display. An unrecognised payer is not fatal —
    it still ingests — but it is logged, because a name that failed to line up
    otherwise looks exactly like a payer we hold no policy for.
    """
    slug = normalize_payer(policy.payer)
    if not is_known_payer(slug):
        logger.warning(
            "Seeding %s under unrecognised payer slug %r (from %r) — add it to "
            "packages/payer-vocab if this is a payer we mean to support",
            policy.policy_id,
            slug,
            policy.payer,
        )

    fields = {
        "policy_id": policy.policy_id,
        "payer": slug,
        "content_type": policy.content_type,
        "source_url": policy.source_url,
    }
    if policy.state is not None:
        fields["state"] = policy.state
    return fields


async def seed_one(
    fetcher: PoliteClient,
    ingest_client: httpx.AsyncClient,
    *,
    base_url: str,
    policy: SeedPolicy,
) -> str:
    """Fetch one policy and upload it, returning ingest's status label.

    Raises:
        SeedFailed: The document could not be fetched, or ingest rejected it.
    """
    try:
        body = await fetcher.get(policy.source_url)
    except (RobotsDisallowed, httpx.HTTPError) as exc:
        raise SeedFailed(f"Could not fetch {policy.policy_id}: {exc}") from exc

    if not body.strip():
        raise SeedFailed(f"{policy.policy_id} fetched as an empty document")

    response = await ingest_client.post(
        f"{base_url}/policies/ingest",
        data=_form_fields(policy),
        files={"file": (policy.filename, body, policy.content_type)},
    )
    if response.status_code != httpx.codes.OK:
        raise SeedFailed(
            f"Ingest of {policy.policy_id} failed with HTTP {response.status_code}: "
            f"{response.text[:500]}"
        )

    payload = response.json().get("data")
    if not isinstance(payload, dict) or "status" not in payload:
        raise SeedFailed(f"Ingest of {policy.policy_id} returned an unrecognised body")

    status = str(payload["status"])
    logger.info(
        "%s: %s (%s chunks indexed)",
        policy.policy_id,
        status,
        payload.get("chunks_indexed", 0),
    )
    return status


async def seed(base_url: str, policies: tuple[SeedPolicy, ...] = SEED_POLICIES) -> int:
    """Seed every policy, returning the number that failed.

    One failing document does not abort the run — a payer reorganising a single
    URL should not cost the whole corpus — but the count comes back so the
    caller can exit non-zero. A run that quietly indexed nothing looks exactly
    like a run with nothing to do, which is the failure this reports around.
    """
    failures = 0
    async with (
        PoliteClient(
            user_agent=USER_AGENT,
            delay_seconds=DELAY_SECONDS,
            timeout_seconds=TIMEOUT_SECONDS,
        ) as fetcher,
        httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as ingest_client,
    ):
        for policy in policies:
            try:
                await seed_one(fetcher, ingest_client, base_url=base_url, policy=policy)
            except SeedFailed:
                logger.exception("Seeding %s failed", policy.policy_id)
                failures += 1

    logger.info("Seeded %d of %d documents", len(policies) - failures, len(policies))
    return failures


def main() -> int:
    """Entry point. Returns a process exit code."""
    parser = argparse.ArgumentParser(description="Seed dev Qdrant with payer policies.")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("TRACK_B_RAG_URL", DEFAULT_TRACK_B_RAG_URL),
        help="Where track-b-rag is reachable (default: $TRACK_B_RAG_URL).",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    return 1 if asyncio.run(seed(args.base_url)) else 0


if __name__ == "__main__":
    sys.exit(main())
