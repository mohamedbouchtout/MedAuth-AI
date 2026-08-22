"""The procedure codes this scraper collects policies for.

Scoping the scrape is not an optimisation. CMS publishes 949 current LCDs and
357 NCDs; indexing all of them would spend embedding time on durable medical
equipment and oncology infusion policies that a private orthopedic or
dermatology practice will never order against. Filtering to the codes MedAuth
actually sees resolves to roughly eighteen LCDs — few enough to be a polite
nightly job, specific enough to be a real corpus.

**Two facts about this list that are easy to get wrong, and were:**

* ``72148`` is MRI of the *lumbar spine*. It is not a knee MRI, whatever an
  earlier draft of TASKS.md called it; knee MRI without contrast is ``73721``.
  Both belong here.
* ``29881`` — knee arthroscopy with meniscectomy — is deliberately **absent**.
  It has no CMS coverage document at all: no LCD, no billing article. Neither do
  the dermatology biologics (Cosentyx, Taltz, Skyrizi, Stelara, Dupixent). They
  are not missing from this filter; they are missing from Medicare's coverage
  database, because they are commercial and pharmacy-benefit territory. Do not
  widen this list hunting for them — that is TASK-014's job, against Aetna and
  BCBS.
"""

from __future__ import annotations

from typing import Final

#: Code to why it is here. The comment is the point: a bare list of codes rots
#: the moment someone wonders whether one of them still earns its place.
TARGET_CODES: Final[dict[str, str]] = {
    "72148": "MRI, lumbar spine, without contrast — the highest-volume imaging order we see",
    "72149": "MRI, lumbar spine, with contrast",
    "73721": "MRI, any joint of lower extremity, without contrast — knee MRI",
    "73718": "MRI, lower extremity other than joint, without contrast",
    "20610": "Arthrocentesis/injection, major joint — the injection half of knee OA care",
    "J7321": "Hyaluronan injection (Hyalgan, Supartz, Visco-3), knee",
    "J7325": "Hyaluronan injection (Synvisc, Synvisc-One), knee",
    "64483": "Injection, transforaminal epidural, lumbar or sacral, single level",
    "62323": "Injection, interlaminar epidural, lumbar or sacral, with imaging",
    "Q5121": "Infliximab-axxq (Avsola) — the one biologic with real CMS coverage documents",
}
