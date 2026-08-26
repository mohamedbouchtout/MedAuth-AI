"""Keyword-to-CPT resolution: what it answers, and what it refuses to answer.

The refusals carry as much weight as the answers here. A guessed code writes a
cacheable payer-policy answer under a key standing for a different procedure, so
every "no" in this suite is a deliberate behaviour and not an unhandled case.
"""

from __future__ import annotations

import pytest

from track_b_rag.keywords import detect_procedures
from track_b_rag.procedure_codes import (
    AXIS_NOT_SPOKEN,
    KEYWORD_RULES,
    NEVER_CODED,
    REASON_AXIS_NOT_SPOKEN,
    REASON_NO_CPT_EXISTS,
    REASON_QUALIFIER_NOT_STATED,
    REASON_QUALIFIER_UNMAPPED,
    NoProcedureCode,
    ProcedureCode,
    resolve_procedure_code,
)


def resolve(text: str) -> ProcedureCode | NoProcedureCode:
    """Resolve the single procedure mentioned in `text`, through the real detector.

    Going through :func:`detect_procedures` rather than building a
    ``ProcedureMention`` by hand is deliberate: the excerpt and the matched text
    are what the resolver anchors on, and a hand-built mention would let this
    suite pass while the two modules disagreed about either.
    """
    (mention,) = detect_procedures(text)
    return resolve_procedure_code(mention)


class TestCodesThatResolve:
    """The table's positive answers."""

    @pytest.mark.parametrize(
        ("text", "cpt_code"),
        [
            ("Let's get an MRI of the left knee.", "73721"),
            ("I want an MRI of that hip.", "73721"),
            ("We should MRI the ankle.", "73721"),
            ("Order an MRI of the right shoulder.", "73221"),
            ("An MRI of the elbow, please.", "73221"),
            ("Let's do an MRI of the wrist.", "73221"),
            ("She needs an MRI of the lumbar spine.", "72148"),
            ("Get an MRI of the low back.", "72148"),
            ("An MRI of the cervical spine is next.", "72141"),
            ("We will order an MRI of the brain.", "70551"),
            ("Let's get a CT scan of the head.", "70450"),
            ("A CT scan of the chest, then.", "71250"),
            ("Order a CT scan of the abdomen and pelvis.", "74176"),
            ("A CT scan of the abdomen should show it.", "74150"),
            ("Let's get a CT scan of the pelvis.", "72192"),
            ("Schedule an echocardiogram.", "93306"),
            ("Let's do a transthoracic echocardiogram.", "93306"),
            ("Schedule a nuclear stress test.", "78452"),
            ("We'll book a treadmill stress test.", "93015"),
        ],
    )
    def test_a_stated_qualifier_selects_its_code(self, text: str, cpt_code: str) -> None:
        result = resolve(text)

        assert isinstance(result, ProcedureCode)
        assert result.cpt_code == cpt_code

    def test_the_body_part_is_used_rather_than_a_generic_imaging_code(self) -> None:
        """TASK-024's headline test: "MRI of the left knee" is not a generic MRI.

        A generic code would be the exact failure the task forbids — one cache
        key standing for every MRI, answered once and served to every later
        encounter that mentions one.
        """
        knee = resolve("The knee has been locking for months. Let's get an MRI of the left knee.")
        brain = resolve("Let's get an MRI of the brain.")

        assert isinstance(knee, ProcedureCode)
        assert isinstance(brain, ProcedureCode)
        assert knee.cpt_code == "73721"
        assert knee.cpt_code != brain.cpt_code

    def test_the_nearest_body_site_wins_when_two_are_named(self) -> None:
        """ "The shoulder is fine, let's MRI the knee" is a knee MRI."""
        result = resolve("The shoulder looks fine now, so let's get an MRI of the knee.")

        assert isinstance(result, ProcedureCode)
        assert result.cpt_code == "73721"

    def test_a_longer_qualifier_beats_the_one_nested_inside_it(self) -> None:
        """ "abdomen and pelvis" is its own code, not the abdomen code."""
        both = resolve("Order a CT scan of the abdomen and pelvis.")
        abdomen_only = resolve("Order a CT scan of the abdomen.")

        assert isinstance(both, ProcedureCode)
        assert isinstance(abdomen_only, ProcedureCode)
        assert both.cpt_code == "74176"
        assert abdomen_only.cpt_code == "74150"

    def test_an_unqualified_echocardiogram_takes_the_documented_default(self) -> None:
        """The one keyword with a default, and it is named rather than implied."""
        result = resolve("Let's get an echocardiogram.")

        assert isinstance(result, ProcedureCode)
        assert KEYWORD_RULES["echocardiogram"].default_qualifier == "transthoracic"
        assert result.cpt_code == "93306"

    def test_every_resolvable_code_names_what_it_assumed_or_assumes_nothing(self) -> None:
        """An entry that fixes an unstated axis has to say so.

        This is what stops a later reader from taking "MRI of the knee -> 73721"
        as unconditional when it in fact means the without-contrast study.
        """
        for rule in KEYWORD_RULES.values():
            for qualifier, code in rule.codes.items():
                assert code.cpt_code
                assert code.descriptor
                assert isinstance(code.assumes, tuple), qualifier


class TestRefusals:
    """The four ways a keyword yields no code, kept distinct on purpose."""

    @pytest.mark.parametrize(
        "text",
        [
            "He is a candidate for a biologic.",
            "We are starting chemotherapy.",
            "I'll write a referral to orthopedics.",
        ],
    )
    def test_a_keyword_that_names_no_coded_procedure_is_permanent(self, text: str) -> None:
        result = resolve(text)

        assert isinstance(result, NoProcedureCode)
        assert result.reason == REASON_NO_CPT_EXISTS

    @pytest.mark.parametrize(
        "text",
        [
            "Let's get an X-ray of the knee.",
            "She may need an arthroscopy of that knee.",
            "A cortisone injection in the shoulder should help.",
            "We'll do a biopsy of the lesion.",
        ],
    )
    def test_a_code_selected_by_something_unspoken_is_refused(self, text: str) -> None:
        """Each of these names a real coded procedure, and none is determinable.

        Note the X-ray and arthroscopy cases both state a body site — the site is
        not what is missing, so a site-based table would have answered them
        wrongly. That is why this is its own reason.
        """
        result = resolve(text)

        assert isinstance(result, NoProcedureCode)
        assert result.reason == REASON_AXIS_NOT_SPOKEN

    @pytest.mark.parametrize(
        "text",
        [
            "Let's get an MRI.",
            "We should get a CT scan first.",
            "Schedule a stress test.",
        ],
    )
    def test_a_missing_qualifier_produces_no_query_and_no_placeholder(self, text: str) -> None:
        """TASK-024: a keyword with no confident mapping produces no code at all."""
        result = resolve(text)

        assert isinstance(result, NoProcedureCode)
        assert result.reason == REASON_QUALIFIER_NOT_STATED
        assert result.keyword in {"MRI", "CT scan", "stress test"}

    def test_a_bare_stress_test_is_refused_rather_than_read_as_the_cheapest(self) -> None:
        """The three stress modalities differ in how hard payers gate them.

        Defaulting a bare "stress test" to the exercise code would understate the
        authorization requirement for the nuclear study, which is the direction
        this pipeline must never fail in.
        """
        bare = resolve("Schedule a stress test.")
        nuclear = resolve("Schedule a nuclear stress test.")

        assert isinstance(bare, NoProcedureCode)
        assert isinstance(nuclear, ProcedureCode)
        assert KEYWORD_RULES["stress test"].default_qualifier is None

    def test_a_recognised_qualifier_with_no_entry_says_extend_the_table(self) -> None:
        """A TEE is recognised and deliberately uncoded — distinct from unstated."""
        result = resolve("Let's arrange a transesophageal echocardiogram.")

        assert isinstance(result, NoProcedureCode)
        assert result.reason == REASON_QUALIFIER_UNMAPPED

    def test_a_keyword_with_no_rule_at_all_is_reported_as_unmapped(self) -> None:
        """Adding a keyword to the detector without a rule here lands here.

        Constructed directly rather than through the detector, because the point
        is a keyword the detector does not yet produce.
        """
        from track_b_rag.keywords import ProcedureMention

        result = resolve_procedure_code(
            ProcedureMention(
                keyword="colonoscopy",
                procedure="colonoscopy",
                matched_text="colonoscopy",
                excerpt="Let's schedule a colonoscopy.",
            )
        )

        assert isinstance(result, NoProcedureCode)
        assert result.reason == REASON_QUALIFIER_UNMAPPED


class TestTableIntegrity:
    """Properties the table has to keep as it grows."""

    def test_every_detector_keyword_has_a_decision_recorded(self) -> None:
        """No keyword may fall through silently.

        A keyword the detector fires on must appear in exactly one of the three
        tables. Falling through would resolve as "unmapped", which is a truthful
        answer but hides that nobody ever decided.
        """
        detected = {
            mention.keyword
            for text in (
                "MRI CT scan X-ray biopsy injection arthroscopy echocardiogram",
                "stress test biologic chemotherapy",
                "referral to orthopedics",
            )
            for mention in detect_procedures(text)
        }

        decided = set(KEYWORD_RULES) | set(NEVER_CODED) | set(AXIS_NOT_SPOKEN)
        assert detected <= decided, detected - decided

    def test_the_three_tables_do_not_overlap(self) -> None:
        """One keyword, one decision — otherwise the resolution order is the rule."""
        assert not set(KEYWORD_RULES) & set(NEVER_CODED)
        assert not set(KEYWORD_RULES) & set(AXIS_NOT_SPOKEN)
        assert not set(NEVER_CODED) & set(AXIS_NOT_SPOKEN)

    def test_every_coded_qualifier_is_one_the_matcher_can_find(self) -> None:
        """A code keyed on a qualifier with no pattern is unreachable."""
        for keyword, rule in KEYWORD_RULES.items():
            recognised = {name for name, _ in rule.qualifiers}
            assert set(rule.codes) <= recognised, keyword

    def test_a_default_qualifier_is_always_one_that_has_a_code(self) -> None:
        """A default pointing at an uncoded qualifier would refuse every mention."""
        for keyword, rule in KEYWORD_RULES.items():
            if rule.default_qualifier is not None:
                assert rule.default_qualifier in rule.codes, keyword

    def test_no_two_qualifiers_of_one_keyword_share_a_code(self) -> None:
        """Distinct codes stay distinct, except where one code genuinely covers many.

        73721 covers any lower extremity joint and 73221 any upper one, so knee,
        hip and ankle sharing a code is a property of the code and not a
        copy-paste. Anything else sharing would collapse two procedures onto one
        cache key.
        """
        shared_by_design = {"73721", "73221"}
        for keyword, rule in KEYWORD_RULES.items():
            codes = [code.cpt_code for code in rule.codes.values()]
            unshared = [code for code in codes if code not in shared_by_design]
            assert len(unshared) == len(set(unshared)), keyword
