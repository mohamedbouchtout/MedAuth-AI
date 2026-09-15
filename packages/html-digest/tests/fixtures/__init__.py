"""Real responses captured from a payer, not hand-written HTML.

``aetna/cpb-1009-first-fetch.html`` and ``aetna/cpb-1009-second-fetch.html`` are
two consecutive fetches of Aetna Clinical Policy Bulletin 1009
(``https://www.aetna.com/cpb/medical/data/1000_1099/1009.html``), taken on
2026-09-14 through ``scripts/seed-policies.py``'s own ``PoliteClient`` and
committed byte for byte. They are the smallest of the nine Aetna documents in
the dev corpus.

They are what TASK-009 is about. The two files are the same length and differ
only inside the Akamai mPulse ``<script>`` the CDN injects with a per-request
token, so their raw SHA-256 digests differ while their policy text does not.
A hand-written page would not have carried that script, and a test built on one
could not have caught the bug.

``.gitattributes`` marks this directory ``-text`` so that git's line-ending
conversion can never rewrite them: these tests are about exact bytes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

FIXTURES: Final = Path(__file__).resolve().parent

#: Two fetches of the same Aetna page, seconds apart.
AETNA_FIRST_FETCH: Final = FIXTURES / "aetna" / "cpb-1009-first-fetch.html"
AETNA_SECOND_FETCH: Final = FIXTURES / "aetna" / "cpb-1009-second-fetch.html"
