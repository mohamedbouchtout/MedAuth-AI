"""Athenahealth. First EHR to certify against — see CLAUDE.md's priority order."""

from __future__ import annotations

from .base import EHRAdapter


class AthenaAdapter(EHRAdapter):
    """Athenahealth's adapter.

    No overrides yet. TASK-054 overrides ``submit_prior_auth()``: Athenahealth
    does not support FHIR PAS, so prior authorizations go through the
    CoverMyMeds API instead. Everything else is standard FHIR and stays on the
    base class. TASK-055 records any further Athenahealth quirks here.
    """
