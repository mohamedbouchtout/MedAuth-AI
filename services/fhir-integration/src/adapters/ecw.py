"""eClinicalWorks."""

from __future__ import annotations

from .base import EHRAdapter


class ECWAdapter(EHRAdapter):
    """eClinicalWorks' adapter.

    No overrides yet. Expected to need minor coverage field handling; nothing
    is written here until a real sandbox response shows what actually differs.
    """
