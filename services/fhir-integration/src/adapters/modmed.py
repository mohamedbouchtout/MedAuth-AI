"""Modernizing Medicine (EMA) — dermatology and orthopedics, our exact specialties."""

from __future__ import annotations

from .base import EHRAdapter


class ModMedAdapter(EHRAdapter):
    """Modernizing Medicine's adapter.

    No overrides yet. TASK-058 validates against the EMA sandbox and adds any
    EMA-specific extension handling that turns out to be needed.
    """
