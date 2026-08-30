"""The EHR adapter layer.

Callers import ``get_adapter()`` and ``detect_ehr_from_issuer()`` from here and
nothing else selects an adapter. The vendor subclasses are deliberately not
exported: a route that can name ``EpicAdapter`` is a route that can hardcode an
EHR, which is what this layer exists to prevent.
"""

from __future__ import annotations

from .base import EHRAdapter
from .factory import EHRType, detect_ehr_from_issuer, get_adapter
from .models import CoverageInfo, PatientContext, PatientInfo, PriorAuthSubmission

__all__ = [
    "CoverageInfo",
    "EHRAdapter",
    "EHRType",
    "PatientContext",
    "PatientInfo",
    "PriorAuthSubmission",
    "detect_ehr_from_issuer",
    "get_adapter",
]
