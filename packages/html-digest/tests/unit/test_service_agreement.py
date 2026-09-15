"""track-b-rag and policy-scraper compute one digest — proven, not assumed (TASK-009).

policy-scraper hashes each document itself before uploading and skips the
upload when its digest equals ``insurance_policies.content_hash``. The stored
value comes from track-b-rag's ingest. The skip is therefore only correct while
the two services compute the same thing, and if they drift nothing fails: the
scraper uploads every document every night and ingest answers ``unchanged``
each time.

Both call :func:`html_digest.html_digest` today, but that is an implementation
detail either side could change. So this file calls each service's *own* entry
point — ``track_b_rag.documents.content_digest`` with the type the scraper
declares, and ``policy_scraper.documents.PolicyDocument.content_hash`` — on the
same inputs, including two real fetches of an Aetna page. Two suites that each
pass on their own side would not prove this.

It lives here rather than in either service for the reason session-auth's issuer
contract test does: the agreement belongs to the definition, not to one of its
consumers.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from policy_scraper.documents import PolicyDocument
from policy_scraper.ingest import upload
from tests.fixtures import AETNA_FIRST_FETCH, AETNA_SECOND_FETCH
from track_b_rag.documents import content_digest

#: Inputs both services can meet: a CMS export fragment as the scraper assembles
#: one, and the same shapes carrying what the digest now ignores or must not.
SAMPLES: dict[str, bytes] = {
    "cms-fragment": (
        b"<h1>Lumbar MRI</h1>\n<p>Covered after six weeks of conservative therapy.</p>"
    ),
    "script": b"<p>Six weeks.</p><script>t='a1'</script>",
    "style": b"<style>p { color: red }</style><p>Six weeks.</p>",
    "commented-out-script": b"<!-- <script>x()</script> --><p>Six weeks.</p>",
    "unterminated-script": b"<p>Six weeks.</p><script>never closed",
    "cp1252": "<p>the payer’s criteria</p><script>x()</script>".encode("cp1252"),
}


def scraper_digest(body: bytes) -> str:
    """What policy-scraper compares against the stored digest before uploading."""
    return PolicyDocument(
        policy_id="cms-lcd-L00000",
        title="Agreement sample",
        body=body,
        states=[],
        source_url="https://example.test/lcd",
        effective_date=None,
    ).content_hash


def ingest_digest(body: bytes) -> str:
    """What track-b-rag stores for the same bytes, declared as the scraper declares them."""
    return content_digest(body, "text/html")


@pytest.mark.parametrize("body", list(SAMPLES.values()), ids=list(SAMPLES))
def test_both_services_compute_the_same_digest(body: bytes) -> None:
    assert scraper_digest(body) == ingest_digest(body)


@pytest.mark.parametrize(
    "fetch", [AETNA_FIRST_FETCH, AETNA_SECOND_FETCH], ids=["first-fetch", "second-fetch"]
)
def test_both_services_agree_on_a_real_aetna_page(fetch: Path) -> None:
    body = fetch.read_bytes()

    assert scraper_digest(body) == ingest_digest(body)


def test_two_real_fetches_are_one_document_to_both_services() -> None:
    first, second = AETNA_FIRST_FETCH.read_bytes(), AETNA_SECOND_FETCH.read_bytes()

    assert scraper_digest(first) == scraper_digest(second)
    assert ingest_digest(first) == ingest_digest(second)


async def test_what_the_scraper_uploads_digests_to_what_it_skipped_on() -> None:
    """The comparisons above declare ``text/html`` because the scraper does —
    checked here from its real upload rather than assumed. Were it ever to
    declare another type, ingest would hash differently and the skip would never
    match again."""
    body = SAMPLES["script"]
    document = PolicyDocument(
        policy_id="cms-lcd-L00000",
        title="Agreement sample",
        body=body,
        states=[],
        source_url="https://example.test/lcd",
        effective_date=None,
    )
    sent: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(
            200,
            json={
                "data": {"status": "created", "content_hash": "x", "chunks_indexed": 1},
                "error": None,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
        await upload(client, base_url="http://rag.test", document=document)

    multipart = sent[0].content
    assert b'name="content_type"\r\n\r\ntext/html' in multipart
    assert body in multipart
    assert content_digest(body, "text/html") == document.content_hash
