"""Building and storing one encounter's bundle.

``build_bundle`` and ``claimable_diagnoses`` are pure and tested directly.
``store_bundle`` is exercised against a fake session that reproduces the two
outcomes the real ``ON CONFLICT DO NOTHING`` insert has: a row, or None.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from prior_auth import assembly
from tests.unit.factories import a_code, a_note, a_nudge, a_suggested_code, an_encounter
from track_a_clinical.models import SOURCE_PROVIDER_ACCEPTED


class FakeSession:
    """Just enough AsyncSession for :func:`assembly.store_bundle`.

    ``scalar`` returns whatever the insert was told to return, which is how the
    duplicate-delivery path is driven without a database.
    """

    def __init__(self, returns: uuid.UUID | None) -> None:
        self._returns = returns
        self.statements: list[Any] = []
        self.commits = 0
        self.rollbacks = 0

    async def scalar(self, statement: Any) -> uuid.UUID | None:
        self.statements.append(statement)
        return self._returns

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


@pytest.fixture
def audited(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Record audit calls instead of writing them, and return the record."""
    calls: list[dict[str, Any]] = []

    async def fake_audit(_session: Any, **kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(assembly.audit, "audit_bundle_write", fake_audit)
    return calls


class TestProcedures:
    def test_a_coded_nudge_becomes_a_procedure(self) -> None:
        bundle = assembly.build_bundle(
            note=a_note(),
            nudges=[a_nudge()],
            encounter_payer="Aetna",
            coverage_payer=None,
        )
        assert bundle.procedures == [{"cpt_code": "73721", "description": "knee MRI"}]

    def test_an_uncoded_nudge_is_excluded_and_counted(self) -> None:
        """A payer cannot be asked to authorize a procedure with no code.

        ``PriorAuthProcedure.cpt_code`` is required at the submission boundary,
        so such an entry would fail there rather than here. TASK-044 raises
        these on a keyword that resolved to no code.
        """
        bundle = assembly.build_bundle(
            note=a_note(),
            nudges=[a_nudge(cpt_code=None, procedure_name="biopsy"), a_nudge()],
            encounter_payer="Aetna",
            coverage_payer=None,
        )
        assert bundle.procedures == [{"cpt_code": "73721", "description": "knee MRI"}]
        assert bundle.uncoded_nudges == 1

    def test_a_whitespace_only_code_counts_as_uncoded(self) -> None:
        bundle = assembly.build_bundle(
            note=a_note(),
            nudges=[a_nudge(cpt_code="  ")],
            encounter_payer=None,
            coverage_payer=None,
        )
        assert bundle.procedures == []
        assert bundle.uncoded_nudges == 1

    def test_a_repeated_code_appears_once(self) -> None:
        bundle = assembly.build_bundle(
            note=a_note(),
            nudges=[a_nudge(), a_nudge()],
            encounter_payer=None,
            coverage_payer=None,
        )
        assert len(bundle.procedures) == 1

    def test_evidence_covers_only_the_coded_nudges(self) -> None:
        """The bundle never offers documentation for a procedure it is not requesting."""
        bundle = assembly.build_bundle(
            note=a_note(),
            nudges=[
                a_nudge(cpt_code=None, procedure_name="biopsy", nudge_message="Biopsy flagged."),
                a_nudge(missing_criteria=[]),
            ],
            encounter_payer=None,
            coverage_payer=None,
        )
        assert [entry["text"] for entry in bundle.clinical_evidence] == [
            "Prior authorization required for knee MRI."
        ]


class TestDiagnoses:
    def test_llm_extracted_codes_are_claimable(self) -> None:
        note = a_note(icd10_codes=[a_code("M17.11")])
        assert [code["code"] for code in assembly.claimable_diagnoses(note)] == ["M17.11"]

    def test_a_comprehend_medical_suggestion_is_never_claimed(self) -> None:
        """A bundle asserts what the provider documented, not what a machine read.

        CLAUDE.md's shape contract is explicit that TASK-060 does not put such a
        code in a bundle as a diagnosis.
        """
        note = a_note(icd10_codes=[a_code("M17.11"), a_suggested_code("M25.561")])
        assert [code["code"] for code in assembly.claimable_diagnoses(note)] == ["M17.11"]

    def test_the_same_code_accepted_by_a_provider_is_claimable(self) -> None:
        """The mechanism by which a suggestion becomes claimable: a human decided."""
        note = a_note(icd10_codes=[a_code("M25.561", source=SOURCE_PROVIDER_ACCEPTED)])
        assert [code["code"] for code in assembly.claimable_diagnoses(note)] == ["M25.561"]

    def test_a_null_column_and_an_empty_one_both_give_no_diagnoses(self) -> None:
        assert assembly.claimable_diagnoses(a_note(icd10_codes=None)) == []
        assert assembly.claimable_diagnoses(a_note(icd10_codes=[])) == []

    def test_no_note_means_no_diagnoses(self) -> None:
        assert assembly.claimable_diagnoses(None) == []


class TestPayerName:
    def test_the_encounter_column_wins(self) -> None:
        bundle = assembly.build_bundle(
            note=a_note(), nudges=[a_nudge()], encounter_payer="Aetna", coverage_payer="AETNA INC"
        )
        assert bundle.payer_name == "Aetna"

    def test_the_chart_context_fills_a_missing_one(self) -> None:
        bundle = assembly.build_bundle(
            note=a_note(), nudges=[a_nudge()], encounter_payer=None, coverage_payer="Aetna"
        )
        assert bundle.payer_name == "Aetna"

    def test_neither_leaves_it_unset(self) -> None:
        bundle = assembly.build_bundle(
            note=a_note(), nudges=[a_nudge()], encounter_payer=None, coverage_payer=None
        )
        assert bundle.payer_name is None


class TestStoring:
    async def test_a_stored_bundle_is_audited_on_the_same_transaction(
        self, audited: list[dict[str, Any]]
    ) -> None:
        encounter = an_encounter()
        request_id = uuid.uuid4()
        session = FakeSession(returns=request_id)
        bundle = assembly.build_bundle(
            note=a_note(), nudges=[a_nudge()], encounter_payer="Aetna", coverage_payer=None
        )

        stored = await assembly.store_bundle(session, encounter=encounter, bundle=bundle)  # type: ignore[arg-type]

        assert stored == request_id
        assert session.commits == 1
        assert audited == [
            {
                "request_id": request_id,
                "session_id": encounter.session_id,
                "provider_id": encounter.provider_id,
            }
        ]

    async def test_a_duplicate_delivery_writes_nothing_and_audits_nothing(
        self, audited: list[dict[str, Any]]
    ) -> None:
        """The ON CONFLICT DO NOTHING path.

        An audit row claiming a PHI write that did not happen is worse than no
        row, so the suppressed insert records neither.
        """
        session = FakeSession(returns=None)
        bundle = assembly.build_bundle(
            note=a_note(), nudges=[a_nudge()], encounter_payer=None, coverage_payer=None
        )

        stored = await assembly.store_bundle(session, encounter=an_encounter(), bundle=bundle)  # type: ignore[arg-type]

        assert stored is None
        assert audited == []
        assert session.commits == 0
        assert session.rollbacks == 1

    async def test_the_insert_names_the_unique_constraint(
        self, audited: list[dict[str, Any]]
    ) -> None:
        """Named rather than inferred, so a rename one service over breaks loudly."""
        session = FakeSession(returns=uuid.uuid4())
        bundle = assembly.build_bundle(
            note=a_note(), nudges=[a_nudge()], encounter_payer=None, coverage_payer=None
        )

        await assembly.store_bundle(session, encounter=an_encounter(), bundle=bundle)  # type: ignore[arg-type]

        compiled = str(session.statements[0].compile())
        assert "uq_prior_auth_requests_encounter" in compiled
        assert "DO NOTHING" in compiled.upper()
