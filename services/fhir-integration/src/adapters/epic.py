"""Epic. Last in the priority order — hardest certification, largest market."""

from __future__ import annotations

from .base import EHRAdapter


class EpicAdapter(EHRAdapter):
    """Epic's adapter.

    No overrides yet. TASK-057 overrides ``get_patient_context()`` to enrich it
    with Epic's proprietary extensions. That enrichment is additive: the base
    behaviour is a complete answer without it, so nothing above the adapter
    layer may depend on an Epic extension being present.
    """
