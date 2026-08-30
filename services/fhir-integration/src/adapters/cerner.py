"""Cerner, now Oracle Health. Both names appear in real issuer URLs."""

from __future__ import annotations

from .base import EHRAdapter


class CernerAdapter(EHRAdapter):
    """Cerner's adapter.

    No overrides yet. TASK-056 overrides ``get_patient_context()`` to fall back
    when the ``Coverage`` resource comes back with incomplete payer information
    — the composed method rather than ``get_coverage()``, so the override can
    call ``super()`` and adjust rather than repeat three fetches.
    """
