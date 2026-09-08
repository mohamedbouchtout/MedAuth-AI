"""``clinical_evidence`` comes from the note and the nudges, never a transcript.

The first class here is the regression guard on TASK-060's respecification. The
rest cover the narrowing that makes an entry minimum-necessary rather than the
whole note.
"""

from __future__ import annotations

import ast
import inspect
from types import ModuleType

import pytest

from prior_auth import assembly, consumer, evidence
from tests.unit.factories import a_note, a_nudge


def _executable_strings(source: str) -> list[str]:
    """Return every string literal in `source` that is not a docstring."""
    tree = ast.parse(source)
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


class TestTheSourceIsTheNoteNotTheTranscript:
    """The correction itself, asserted rather than only documented."""

    def test_evidence_text_is_drawn_from_the_note(self) -> None:
        nudge = a_nudge(missing_criteria=["six weeks of conservative therapy"])
        entries = evidence.build_evidence(a_note(), [nudge])

        criterion_entry = next(e for e in entries if e["criterion"] is not None)
        assert "six weeks of physical therapy" in criterion_entry["text"]

    @pytest.mark.parametrize("module", [evidence, assembly, consumer])
    def test_no_executable_code_in_this_service_names_a_transcript(
        self, module: ModuleType
    ) -> None:
        """A structural guard, because the tempting fix is to add one back.

        The transcript is not merely unavailable — the note is the artifact a
        payer is owed, because it is what the provider reviewed and attested to.
        A later change that reaches for encounter speech should fail here and
        find the reasoning in ``prior_auth.evidence``.

        Docstrings are excluded rather than scanned: they are exactly where the
        decision *is* recorded, so a text search over the whole file would fail
        on the documentation that exists to prevent the thing being guarded
        against.
        """
        for literal in _executable_strings(inspect.getsource(module)):
            assert "transcription:" not in literal
            assert "transcript" not in literal.casefold()

    def test_the_consumer_subscribes_to_no_transcript_channel(self) -> None:
        """The subscription set is the whole of what this service listens to."""
        channels = {
            value
            for name, value in vars(consumer).items()
            if name.isupper() and isinstance(value, str) and ":" in value
        }
        assert channels == {"sessions:started", "session:ended:{session_id}"}


class TestOneEntryPerCriterion:
    def test_each_missing_criterion_gets_its_own_entry(self) -> None:
        nudge = a_nudge(missing_criteria=["conservative therapy", "prior radiographs"])
        entries = evidence.build_evidence(a_note(), [nudge])

        criteria = [e["criterion"] for e in entries if e["criterion"] is not None]
        assert criteria == ["conservative therapy", "prior radiographs"]

    def test_the_nudge_message_is_carried_as_an_uncriterioned_entry(self) -> None:
        """The only entry when the criteria came back unknown.

        ``missing_criteria`` is empty on a fallback answer — meaning the
        criteria are *unknown*, not that none are missing — so an evidence list
        that went empty here would lose what the encounter did establish.
        """
        nudge = a_nudge(missing_criteria=[])
        entries = evidence.build_evidence(a_note(), [nudge])

        assert entries == [
            {"text": "Prior authorization required for knee MRI.", "criterion": None}
        ]

    def test_a_nudge_with_neither_criteria_nor_message_contributes_nothing(self) -> None:
        nudge = a_nudge(missing_criteria=None, nudge_message=None)
        assert evidence.build_evidence(a_note(), [nudge]) == []


class TestNarrowing:
    def test_only_sentences_naming_the_procedure_are_carried(self) -> None:
        """Minimum necessary: a payer is owed this request's documentation."""
        excerpts = evidence.note_excerpts(a_note(), a_nudge())

        assert any("knee" in excerpt.casefold() for excerpt in excerpts)
        assert not any("shoulder" in excerpt.casefold() for excerpt in excerpts)

    def test_the_cpt_code_also_matches(self) -> None:
        note = a_note(
            plan="Ordered under 73721 as discussed.",
            subjective=None,
            objective=None,
            assessment=None,
        )
        excerpts = evidence.note_excerpts(note, a_nudge(procedure_name=None))

        assert excerpts == ["Ordered under 73721 as discussed."]

    def test_matching_is_case_insensitive(self) -> None:
        note = a_note(assessment="KNEE MRI ordered.", subjective=None, objective=None, plan=None)
        assert evidence.note_excerpts(note, a_nudge(cpt_code=None)) == ["KNEE MRI ordered."]

    def test_a_note_documenting_nothing_about_the_procedure_yields_no_excerpt(self) -> None:
        """An empty answer is meaningful — it is what an undocumented order is.

        The entry is still written, carrying the criterion, because "unmet and
        undocumented" is the substance of the request rather than an absence to
        hide.
        """
        note = a_note(
            subjective="Patient reports a sore throat.",
            objective=None,
            assessment=None,
            plan=None,
        )
        nudge = a_nudge(missing_criteria=["conservative therapy"])

        entries = evidence.build_evidence(note, [nudge])
        assert entries[0] == {"text": "", "criterion": "conservative therapy"}

    def test_a_duplicate_sentence_across_sections_is_carried_once(self) -> None:
        note = a_note(
            subjective="Knee MRI ordered.",
            objective=None,
            assessment="Knee MRI ordered.",
            plan=None,
        )
        assert evidence.note_excerpts(note, a_nudge()) == ["Knee MRI ordered."]

    def test_excerpts_keep_note_order(self) -> None:
        note = a_note(
            subjective="Knee MRI discussed with the patient.",
            objective=None,
            assessment=None,
            plan="Knee MRI to be scheduled.",
        )
        assert evidence.note_excerpts(note, a_nudge()) == [
            "Knee MRI discussed with the patient.",
            "Knee MRI to be scheduled.",
        ]

    @pytest.mark.parametrize("section", ["subjective", "objective", "assessment", "plan"])
    def test_every_soap_section_is_searched(self, section: str) -> None:
        """The subjective section matters as much as the plan.

        Payers ask about conservative therapy and prior imaging, and a note
        records those as history.
        """
        blank = {"subjective": None, "objective": None, "assessment": None, "plan": None}
        note = a_note(**{**blank, section: "Knee MRI justified."})  # type: ignore[arg-type]
        assert evidence.note_excerpts(note, a_nudge()) == ["Knee MRI justified."]


class TestCriterionMatching:
    """The half that finds the sentences justifying a procedure, not ordering it."""

    def test_a_justifying_sentence_is_found_though_it_never_names_the_procedure(
        self,
    ) -> None:
        """The case that drove this half of the matcher.

        "She has completed six weeks of physical therapy without relief" is the
        documentation a payer evaluates a conservative-therapy criterion
        against, and it says nothing about an MRI.
        """
        excerpts = evidence.note_excerpts(
            a_note(), a_nudge(), criterion="six weeks of conservative therapy"
        )
        assert any("six weeks of physical therapy" in excerpt for excerpt in excerpts)

    def test_a_sentence_sharing_one_word_with_the_criterion_is_not_enough(self) -> None:
        """One common word would pull in any sentence about any treatment."""
        note = a_note(
            subjective="Started therapy for an unrelated shoulder complaint.",
            objective=None,
            assessment=None,
            plan=None,
        )
        excerpts = evidence.note_excerpts(
            note, a_nudge(procedure_name=None, cpt_code=None), criterion="conservative therapy"
        )
        assert excerpts == []

    def test_each_criterion_is_excerpted_separately(self) -> None:
        """A shared blob would offer every criterion the evidence for the others."""
        note = a_note(
            subjective="She completed six weeks of physical therapy without relief.",
            objective="Prior radiographs of the knee were obtained in March.",
            assessment=None,
            plan=None,
        )
        nudge = a_nudge(
            procedure_name=None,
            cpt_code=None,
            missing_criteria=["six weeks conservative therapy", "prior radiographs obtained"],
        )
        entries = evidence.build_evidence(note, [nudge])

        therapy, radiographs = entries[0], entries[1]
        assert "physical therapy" in therapy["text"]
        assert "radiographs" not in therapy["text"]
        assert "radiographs" in radiographs["text"]
        assert "physical therapy" not in radiographs["text"]

    def test_connectives_and_short_words_are_not_search_terms(self) -> None:
        assert evidence.criterion_terms("six weeks of the therapy") == frozenset(
            {"weeks", "therapy"}
        )

    def test_a_criterion_of_only_stopwords_yields_no_terms(self) -> None:
        assert evidence.criterion_terms("has not been documented") == frozenset()


class TestBounds:
    def test_a_long_unbroken_excerpt_is_truncated_on_a_word_boundary(self) -> None:
        long_sentence = "Knee MRI " + ("indicated " * 200)
        note = a_note(assessment=long_sentence, subjective=None, objective=None, plan=None)

        excerpt = evidence.note_excerpts(note, a_nudge())[0]
        assert len(excerpt) <= evidence.MAX_EXCERPT_CHARS + 1
        assert excerpt.endswith("…")
        assert not excerpt.endswith(" …")

    def test_a_long_nudge_message_is_truncated_too(self) -> None:
        nudge = a_nudge(missing_criteria=[], nudge_message="x" * 5_000)
        entry = evidence.build_evidence(None, [nudge])[0]
        assert len(entry["text"]) <= evidence.MAX_EXCERPT_CHARS + 1


class TestDegenerateInputs:
    def test_no_note_still_produces_the_nudge_entries(self) -> None:
        """The caller decides whether to assemble without a note; this does not."""
        nudge = a_nudge(missing_criteria=["conservative therapy"])
        entries = evidence.build_evidence(None, [nudge])

        assert [e["criterion"] for e in entries] == ["conservative therapy", None]
        assert entries[0]["text"] == ""

    def test_a_nudge_with_no_searchable_term_yields_no_excerpt(self) -> None:
        assert evidence.note_excerpts(a_note(), a_nudge(procedure_name=None, cpt_code=None)) == []

    def test_whitespace_only_terms_are_ignored(self) -> None:
        assert evidence.note_excerpts(a_note(), a_nudge(procedure_name="  ", cpt_code=" ")) == []

    def test_no_nudges_means_no_evidence(self) -> None:
        assert evidence.build_evidence(a_note(), []) == []
