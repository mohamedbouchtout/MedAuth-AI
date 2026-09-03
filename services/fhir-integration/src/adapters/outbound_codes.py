"""Which extracted clinical codes may leave this system at all.

One rule, in one place, because there are now two outbound writers and each must
apply it: the note write-back composes a ``DocumentReference`` for a patient's
chart (TASK-053) and the prior-auth submission composes a Claim for a payer
(TASK-054). It lived in ``note_document`` while there was one writer and moved
here when the second arrived — the same trigger, and the same argument, as
``packages/api-envelope``'s extraction one task after a second service copied
the envelope.

It is a module of this service rather than a shared package because both callers
are here. A third consumer in another service is what would make it a package.

See CLAUDE.md, "Writing clinical data out to the EHR", which is where the rule
itself is settled.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from .models import NoteCode

#: The two ``source`` values a code may carry and still leave this system.
#:
#: A ``comprehend-medical`` entry is deliberately absent, and this is the rule
#: rather than a conservative default: that source means the validating pass
#: surfaced a code **no provider ever stated**. CLAUDE.md forbids putting one in
#: a prior-auth bundle, where it would assert to a payer something the provider
#: did not document, and applies the rule with more force still to a patient's
#: permanent chart. The way such a code becomes sendable is unchanged — a
#: provider accepts it through ``PATCH /notes/{session_id}``, which rewrites its
#: ``source`` to ``provider-accepted``.
#:
#: Do not widen this set to "everything we extracted". The filter is the point.
SENDABLE_CODE_SOURCES: Final = frozenset({"llm-extraction", "provider-accepted"})


def sendable_codes(codes: Sequence[NoteCode] | None) -> list[NoteCode]:
    """Return only the codes that may be sent outside this system.

    Args:
        codes: The extracted codes, or ``None`` when the extraction pass never
            answered. ``None`` and ``[]`` mean different things upstream — "not
            determined" against "none found" — but they produce the same answer
            here, because neither yields a code a provider documented.

    Returns:
        The entries whose ``source`` is in :data:`SENDABLE_CODE_SOURCES`, in the
        order given.
    """
    return [code for code in codes or () if code.source in SENDABLE_CODE_SOURCES]
