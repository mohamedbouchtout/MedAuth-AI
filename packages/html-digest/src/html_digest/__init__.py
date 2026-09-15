"""Which bytes of an HTML policy document are not document text.

``insurance_policies.content_hash`` decides whether a document is re-embedded.
For HTML it is taken over the uploaded bytes with every ``<script>`` and
``<style>`` element cut out, because a CDN can inject a script carrying a fresh
token into every response: Aetna's pages did, and every seed run reported every
Aetna document ``updated`` while its policy text had not moved (TASK-009).

Three places have to agree about which bytes those are, so all three call this
package rather than each carrying a matcher of its own:

* ``track_b_rag.documents.content_digest`` — the authoritative digest at ingest;
* ``policy_scraper.documents.PolicyDocument.content_hash`` — the scraper's
  pre-upload skip, which is only useful while it equals the digest above;
* ``track_b_rag.markup`` — text extraction, which must discard what the digest
  ignores, or a change the digest cannot see could reach the index.

``tests/unit/test_service_agreement.py`` proves the first two agree by calling
both services' own functions.

**This removes byte ranges; it never re-serialises.** A document with no script
or style comes back as the identical bytes, so its digest is exactly the
SHA-256 of what was published — which is what every digest already stored for
script-free HTML was taken over. See ADR-0021 for the rule this narrows.
"""

from html_digest.strip import (
    NON_TEXT_ELEMENTS,
    html_digest,
    non_text_spans,
    strip_non_text,
)

__all__ = [
    "NON_TEXT_ELEMENTS",
    "html_digest",
    "non_text_spans",
    "strip_non_text",
]
