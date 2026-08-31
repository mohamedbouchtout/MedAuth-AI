"""Which CPT code a spoken procedure keyword actually means.

:mod:`track_b_rag.keywords` turns "let's get an MRI of that knee" into a
:class:`~track_b_rag.keywords.ProcedureMention` carrying the keyword ``"MRI"``.
``POST /policies/query`` needs a CPT code, and that is a different question: a
payer publishes prior-authorization criteria per code, and "MRI" spans dozens of
them by body part and by contrast.

**A wrong code is worse than no code, so this module refuses more than it
answers.** The Redis cache key is ``rag:{payer}:{plan_type}:{state}:{cpt_code}``.
A guessed code does not merely give one encounter a poor answer — it files a
real, cacheable payer-policy answer under a key standing for a different
procedure, and the next encounter matching that key is served it. That failure
is silent and it crosses patients. So the resolver returns
:class:`NoProcedureCode` wherever the spoken phrase does not determine a code,
and the caller does not query at all. Not querying costs one missed nudge for
one order; a wrong code costs a wrong answer for every later encounter that
lands on the key.

**Four distinct ways a keyword yields no code**, because they are not equally
fixable and an operator reading a log line should be able to tell them apart:

``REASON_NO_CPT_EXISTS``
    The keyword names a class of therapy or an administrative act rather than a
    coded procedure. "biologic" is a drug whose HCPCS J-code is specific to the
    agent; "referral" has no CPT code at all. Permanent — no table will fix it.

``REASON_AXIS_NOT_SPOKEN``
    A real coded procedure whose code turns on something nobody says out loud.
    An X-ray's code is selected by the number of views; an arthroscopy's by what
    was found and done inside the joint. Also effectively permanent, but for a
    different reason, and worth separating from the above.

``REASON_QUALIFIER_NOT_STATED``
    The keyword needs a qualifier this module knows how to use — a body site, a
    stress modality — and the excerpt named none. Fixable per encounter: the
    clinician said "let's get an MRI" and not which MRI.

``REASON_QUALIFIER_UNMAPPED``
    A qualifier was recognised and the table has no code for that pairing. This
    is the one that means *extend the table*, and it is the reason the four are
    separated at all.

**What is in the table, and the rule that decides.** An entry exists when the
spoken phrase pins the code down to the level at which payers publish criteria.
Where an unstated axis would change the authorization answer, there is no entry.
Where an entry does fix an unstated axis (an MRI order means "without contrast"
unless someone escalates it), the assumption is named in
:attr:`ProcedureCode.assumes` rather than left implicit, so a reviewer can see
what was decided on their behalf.

**The qualifier axis is not always a body site.** MRI and CT turn on anatomy, a
stress test on modality, an arthroscopy on the intervention the surgeon plans,
and a joint injection on joint size. Reading "which axis selects this code" as
"which body part" is what made an earlier draft of this module exclude
arthroscopy and injection outright — the axis was spoken, it just was not the
one being looked for. Both are mapped here.

**X-ray and biopsy remain unmapped, and that is a decision rather than a gap.**
An X-ray's code is selected by the number of views, which the technologist
chooses at the machine; plain radiography is also essentially never
prior-authorization gated, so a query would spend a Qdrant search and a Sonnet
call to retrieve nothing and report a miss that is indistinguishable from a
corpus we do not hold. "biopsy" spans body systems whose code families have
nothing in common, and the gated ones (breast) split by imaging guidance while
the ones our target specialties order most (skin) split by technique — neither
of which is spoken. See TASK-024 for what would change either.

**The codes in this table are not clinically verified and CPT is AMA-licensed
material.** The descriptors here are short paraphrases, not the AMA's own long
descriptors, and both the codes and the pairings need a certified coder's review
before anything they produce reaches a provider. **That review has not
happened, and since TASK-052b these codes can reach a provider** — the payer
columns are populated, so a query is built and a nudge carrying one of these
codes fires. It is deferred deliberately and narrowly, for a v1 proof of concept
run against synthetic patients on a local FHIR server with no real patient, no
real provider and no submission to a real payer. Issue #70 holds both this and
the AMA CPT licensing position, and no real encounter goes through Track B until
a certified coder has signed off.

**How a new specialty extends this.** The shape is deliberately the one
``packages/payer-vocab`` uses: a deterministic matcher over a curated table,
extended by adding rows rather than by making the matcher cleverer. Adding
dermatology's Mohs codes or orthopedics' remaining joint work means new
:class:`KeywordRule` entries and new keywords in :mod:`track_b_rag.keywords`.
That still needs a deploy, which is a product problem and not only a code one —
a practice cannot add its own procedures today. TASK-024b tracks moving the
table behind a loader so it can be data rather than source.

**Where this lives.** A module in this service, not a package, because
track-b-rag is its only consumer today. ``packages/api-envelope`` was extracted
when a second consumer appeared and not before, and the same trigger applies
here: prior-auth needs procedure codes for bundle assembly (TASK-060), and that
is when this moves to ``packages/procedure-codes``. Unlike the payer slug, a CPT
code is an external identifier this repo does not mint, so nothing stored
depends on where the table lives and the move costs an import.

**PHI.** ``mention.excerpt`` is what was said in an exam room. It is matched
against the qualifier vocabulary here and never logged, and neither is the
qualifier that matched — a body site is clinical fact about a patient. Log lines
about a failed resolution name the keyword and the reason constant, which is
what the rest of this service already permits itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Final

from track_b_rag.keywords import ProcedureMention

REASON_NO_CPT_EXISTS: Final = "no_cpt_exists"
REASON_AXIS_NOT_SPOKEN: Final = "axis_not_spoken"
REASON_QUALIFIER_NOT_STATED: Final = "qualifier_not_stated"
REASON_QUALIFIER_UNMAPPED: Final = "qualifier_unmapped"


@dataclass(frozen=True)
class ProcedureCode:
    """One resolved procedure code.

    Attributes:
        cpt_code: The code itself. Uppercase — HCPCS Level II codes are
            alphanumeric and ``/policies/query`` uppercases what it receives.
        procedure: What to send as the query's ``procedure`` field, phrased the
            way a clinician would recognise it: ``"MRI of the knee"``.
        descriptor: A short paraphrase of what the code covers, so a reviewer can
            check the pairing without a lookup. Deliberately not the AMA's long
            descriptor — see the module docstring on licensing.
        assumes: The axes this entry fixes that the spoken phrase did not.
            ``("without contrast",)`` on an MRI entry says out loud that a
            contrast-enhanced study is a different code and this is not it.
    """

    cpt_code: str
    procedure: str
    descriptor: str
    assumes: tuple[str, ...] = ()


@dataclass(frozen=True)
class NoProcedureCode:
    """Why a mention produced no code.

    Attributes:
        keyword: The canonical keyword, safe to log.
        reason: One of the ``REASON_*`` constants above.
        detail: A sentence explaining the reason, fixed text written here rather
            than derived from the transcript, so it is safe to log in full.
    """

    keyword: str
    reason: str
    detail: str


@dataclass(frozen=True)
class KeywordRule:
    """How one keyword becomes a code.

    Attributes:
        axis: What the qualifier means for this keyword — ``"body site"`` for
            imaging, ``"stress modality"`` for a stress test. Used in the
            ``detail`` text so a log line says what was missing.
        qualifiers: ``(canonical qualifier, regex)`` pairs, matched against the
            excerpt case-insensitively. A closed vocabulary: a site this module
            has never heard of is not a qualifier, it is silence.
        codes: Canonical qualifier to code. A qualifier present in
            ``qualifiers`` but absent here resolves to
            ``REASON_QUALIFIER_UNMAPPED`` — recognised, not yet coded.
        default_qualifier: Which qualifier an unqualified mention means, or None
            when the keyword requires one. Only set where the unqualified phrase
            genuinely has one conventional reading.
        compiled: The qualifier patterns, compiled once at construction.
    """

    axis: str
    qualifiers: tuple[tuple[str, str], ...]
    codes: dict[str, ProcedureCode]
    default_qualifier: str | None = None
    compiled: tuple[tuple[str, re.Pattern[str]], ...] = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "compiled",
            tuple((name, re.compile(pattern, re.IGNORECASE)) for name, pattern in self.qualifiers),
        )


#: Keywords that name no coded procedure at all. Permanent; the value is the
#: explanation that reaches the log line.
NEVER_CODED: Final[dict[str, str]] = {
    "biologic": (
        "a biologic is a drug, not a procedure — its HCPCS J-code is specific to the agent, "
        "and the agent is what a policy query would have to name"
    ),
    "chemotherapy": (
        "chemotherapy administration codes are selected by route and infusion duration, "
        "and the drug itself carries a separate J-code"
    ),
    "referral": (
        "a referral is an administrative act with no CPT code — a plan's referral requirement "
        "is not a procedure authorization"
    ),
}

#: Keywords that do name a coded procedure, whose code turns on something never
#: said out loud in an exam room.
AXIS_NOT_SPOKEN: Final[dict[str, str]] = {
    "X-ray": (
        "the number of views selects the code and is a technologist's decision at the machine; "
        "plain radiography is also not prior-authorization gated, so a query would report a "
        "retrieval miss rather than an answer"
    ),
    "biopsy": (
        "the keyword spans body systems with unrelated code families — the gated ones split by "
        "imaging guidance and the common dermatology ones by technique, and neither is spoken "
        "when the biopsy is proposed"
    ),
}

_MRI_LOWER_JOINT: Final = ProcedureCode(
    cpt_code="73721",
    procedure="MRI of a lower extremity joint",
    descriptor="MRI, any joint of lower extremity, without contrast",
    assumes=("without contrast",),
)
_MRI_UPPER_JOINT: Final = ProcedureCode(
    cpt_code="73221",
    procedure="MRI of an upper extremity joint",
    descriptor="MRI, any joint of upper extremity, without contrast",
    assumes=("without contrast",),
)

#: One rule per keyword that can resolve. Keywords absent from here, from
#: :data:`NEVER_CODED` and from :data:`AXIS_NOT_SPOKEN` are unknown to this
#: module entirely, which is itself a mapping gap rather than a determination.
KEYWORD_RULES: Final[dict[str, KeywordRule]] = {
    "MRI": KeywordRule(
        axis="body site",
        qualifiers=(
            ("lumbar spine", r"\b(?:lumbar(?:\s+spine)?|low(?:er)?\s+back|l-?spine)\b"),
            ("cervical spine", r"\b(?:cervical(?:\s+spine)?|c-?spine)\b"),
            ("brain", r"\b(?:brain|head)\b"),
            ("knee", r"\bknees?\b"),
            ("hip", r"\bhips?\b"),
            ("ankle", r"\bankles?\b"),
            ("shoulder", r"\bshoulders?\b"),
            ("elbow", r"\belbows?\b"),
            ("wrist", r"\bwrists?\b"),
        ),
        codes={
            "knee": _MRI_LOWER_JOINT,
            "hip": _MRI_LOWER_JOINT,
            "ankle": _MRI_LOWER_JOINT,
            "shoulder": _MRI_UPPER_JOINT,
            "elbow": _MRI_UPPER_JOINT,
            "wrist": _MRI_UPPER_JOINT,
            "lumbar spine": ProcedureCode(
                cpt_code="72148",
                procedure="MRI of the lumbar spine",
                descriptor="MRI, lumbar spine, without contrast",
                assumes=("without contrast",),
            ),
            "cervical spine": ProcedureCode(
                cpt_code="72141",
                procedure="MRI of the cervical spine",
                descriptor="MRI, cervical spine, without contrast",
                assumes=("without contrast",),
            ),
            "brain": ProcedureCode(
                cpt_code="70551",
                procedure="MRI of the brain",
                descriptor="MRI, brain, without contrast",
                assumes=("without contrast",),
            ),
        },
    ),
    "CT scan": KeywordRule(
        axis="body site",
        qualifiers=(
            # Longest-first ordering is not enough on its own — see
            # _best_qualifier, which prefers the longer of two overlapping
            # matches so that "abdomen and pelvis" beats the "abdomen" inside it.
            ("abdomen and pelvis", r"\babdomen\s+and\s+pelvis\b"),
            ("abdomen", r"\babdom(?:en|inal)\b"),
            ("pelvis", r"\bpelvi(?:s|c)\b"),
            ("head", r"\b(?:head|brain)\b"),
            ("chest", r"\b(?:chest|thora(?:x|cic))\b"),
        ),
        codes={
            "head": ProcedureCode(
                cpt_code="70450",
                procedure="CT of the head",
                descriptor="CT, head or brain, without contrast",
                assumes=("without contrast",),
            ),
            "chest": ProcedureCode(
                cpt_code="71250",
                procedure="CT of the chest",
                descriptor="CT, thorax, without contrast",
                assumes=("without contrast",),
            ),
            "abdomen and pelvis": ProcedureCode(
                cpt_code="74176",
                procedure="CT of the abdomen and pelvis",
                descriptor="CT, abdomen and pelvis, without contrast",
                assumes=("without contrast",),
            ),
            "abdomen": ProcedureCode(
                cpt_code="74150",
                procedure="CT of the abdomen",
                descriptor="CT, abdomen, without contrast",
                assumes=("without contrast",),
            ),
            "pelvis": ProcedureCode(
                cpt_code="72192",
                procedure="CT of the pelvis",
                descriptor="CT, pelvis, without contrast",
                assumes=("without contrast",),
            ),
        },
    ),
    "echocardiogram": KeywordRule(
        axis="study route",
        qualifiers=(
            ("transesophageal", r"\b(?:transesophageal|t\.?e\.?e\.?)\b"),
            ("transthoracic", r"\b(?:transthoracic|t\.?t\.?e\.?)\b"),
        ),
        codes={
            "transthoracic": ProcedureCode(
                cpt_code="93306",
                procedure="transthoracic echocardiogram",
                descriptor=(
                    "Echocardiography, transthoracic, complete, with spectral and colour "
                    "flow Doppler"
                ),
                assumes=("complete rather than limited", "with Doppler"),
            ),
            # "transesophageal" is a recognised qualifier with no entry on
            # purpose: a TEE is a separate order with its own variants, and
            # reading one as the transthoracic study would be exactly the guess
            # this module exists to refuse.
        },
        #: An unqualified "echocardiogram" in an office encounter means a
        #: transthoracic study; a TEE is scheduled deliberately and named.
        default_qualifier="transthoracic",
    ),
    "arthroscopy": KeywordRule(
        # Not the body site: a knee and a shoulder arthroscopy are different code
        # families, but the intervention names the joint implicitly — a rotator
        # cuff is a shoulder, a meniscus is a knee — so one axis carries both.
        axis="planned intervention",
        qualifiers=(
            ("rotator cuff repair", r"\brotator\s+cuff\b"),
            ("meniscus repair", r"\b(?:meniscus\s+repair|repair\s+(?:the\s+|a\s+)?meniscus)\b"),
            ("meniscectomy", r"\b(?:meniscectomy|trim\s+(?:the\s+)?meniscus)\b"),
            ("chondroplasty", r"\bchondroplast(?:y|ies)\b"),
        ),
        codes={
            "meniscectomy": ProcedureCode(
                cpt_code="29881",
                procedure="knee arthroscopy with meniscectomy",
                descriptor="Arthroscopy, knee, surgical; with meniscectomy, medial OR lateral",
                assumes=("a single compartment — 29880 is the medial AND lateral code",),
            ),
            "meniscus repair": ProcedureCode(
                cpt_code="29882",
                procedure="knee arthroscopy with meniscus repair",
                descriptor="Arthroscopy, knee, surgical; with meniscus repair, medial OR lateral",
                assumes=("a single meniscus — 29883 is the medial AND lateral code",),
            ),
            "rotator cuff repair": ProcedureCode(
                cpt_code="29827",
                procedure="shoulder arthroscopy with rotator cuff repair",
                descriptor="Arthroscopy, shoulder, surgical; with rotator cuff repair",
            ),
            "chondroplasty": ProcedureCode(
                cpt_code="29877",
                procedure="knee arthroscopy with chondroplasty",
                descriptor="Arthroscopy, knee, surgical; with chondroplasty",
            ),
        },
        # No default. "She may need an arthroscopy" names no intervention, and a
        # torn meniscus can be trimmed or repaired — different codes, and which
        # one is a decision that has not been made yet at this point in the
        # visit. A bare "meniscus" therefore selects nothing on purpose.
    ),
    "injection": KeywordRule(
        # Joint size is the axis, and it is spoken far more often than not —
        # "a cortisone injection in the shoulder" names it. Imaging guidance is
        # the axis that is not spoken, and it is handled by `assumes` the same
        # way contrast is on the MRI entries.
        axis="injection site",
        qualifiers=(
            ("major joint", r"\b(?:shoulders?|hips?|knees?|subacromial)\b"),
            (
                "intermediate joint",
                r"\b(?:elbows?|wrists?|ankles?|acromioclavicular|olecranon"
                r"|temporomandibular|t\.?m\.?j\.?)\b",
            ),
            (
                "small joint",
                r"\b(?:fingers?|toes?|interphalangeal|metacarpophalangeal)\b",
            ),
            # Recognised and deliberately uncoded, so these report "extend the
            # table" rather than "nothing was stated". An epidural steroid
            # injection is a spine code selected by level and approach, and a
            # trigger point injection by how many muscles were injected —
            # both are real gaps, unlike a joint nobody named.
            ("epidural", r"\bepidural\b"),
            ("trigger point", r"\btrigger\s+points?\b"),
        ),
        codes={
            "major joint": ProcedureCode(
                cpt_code="20610",
                procedure="major joint injection",
                descriptor="Arthrocentesis, aspiration or injection; major joint or bursa",
                assumes=("without ultrasound guidance — 20611 is the guided code",),
            ),
            "intermediate joint": ProcedureCode(
                cpt_code="20605",
                procedure="intermediate joint injection",
                descriptor="Arthrocentesis, aspiration or injection; intermediate joint or bursa",
                assumes=("without ultrasound guidance — 20606 is the guided code",),
            ),
            "small joint": ProcedureCode(
                cpt_code="20600",
                procedure="small joint injection",
                descriptor="Arthrocentesis, aspiration or injection; small joint or bursa",
                assumes=("without ultrasound guidance — 20604 is the guided code",),
            ),
        },
        # No default: "an injection" spans joint, epidural, tendon sheath and
        # trigger point families, and they are not variants of one another.
    ),
    "stress test": KeywordRule(
        axis="stress modality",
        qualifiers=(
            ("nuclear", r"\b(?:nuclear|myocardial\s+perfusion|spect|thallium|sestamibi)\b"),
            ("echocardiographic", r"\b(?:stress\s+echo(?:cardiograp\w+)?|echo)\b"),
            ("exercise", r"\b(?:exercise|treadmill)\b"),
        ),
        codes={
            "nuclear": ProcedureCode(
                cpt_code="78452",
                procedure="nuclear stress test",
                descriptor="Myocardial perfusion imaging, SPECT, multiple studies",
                assumes=("multiple studies rather than a single study",),
            ),
            "echocardiographic": ProcedureCode(
                cpt_code="93351",
                procedure="stress echocardiogram",
                descriptor="Echocardiography during stress, complete, with interpretation",
                assumes=("complete study including supervision and interpretation",),
            ),
            "exercise": ProcedureCode(
                cpt_code="93015",
                procedure="exercise stress test",
                descriptor=(
                    "Cardiovascular stress test with supervision, interpretation and report"
                ),
                assumes=("the global service rather than a professional-only component",),
            ),
        },
        # No default: the three differ by an order of magnitude in cost and in
        # how hard payers gate them, so reading a bare "stress test" as the
        # cheapest one would understate the authorization requirement.
    ),
}


def _best_qualifier(rule: KeywordRule, excerpt: str, anchor: int) -> str | None:
    """Return the qualifier nearest `anchor`, preferring the longer of two overlaps.

    Args:
        rule: The keyword's rule.
        excerpt: The sentence or two around the mention. PHI — matched, never logged.
        anchor: Where the keyword itself sits in `excerpt`, so that "the shoulder
            looks fine, let's MRI the knee" resolves to the knee and not the
            shoulder. Distance rather than reading order, because the qualifier
            follows the keyword as often as it precedes it.

    Returns:
        The canonical qualifier, or None when the excerpt names none this rule
        knows. The longer-match preference is what makes "abdomen and pelvis"
        win over the "abdomen" nested inside it.
    """
    best: tuple[int, int, str] | None = None
    for name, pattern in rule.compiled:
        for match in pattern.finditer(excerpt):
            distance = min(abs(match.start() - anchor), abs(match.end() - anchor))
            candidate = (distance, -(match.end() - match.start()), name)
            if best is None or candidate < best:
                best = candidate
    return None if best is None else best[2]


def resolve_procedure_code(mention: ProcedureMention) -> ProcedureCode | NoProcedureCode:
    """Resolve one detected mention to a CPT code, or explain why it cannot be.

    Args:
        mention: A procedure keyword found in one transcript segment, with the
            surrounding sentences. The excerpt is PHI.

    Returns:
        A :class:`ProcedureCode` when the spoken phrase determines one, and a
        :class:`NoProcedureCode` carrying a loggable reason otherwise. Never
        raises and never guesses: every branch that cannot answer says which of
        the four kinds of "no" it is.
    """
    keyword = mention.keyword

    if keyword in NEVER_CODED:
        return NoProcedureCode(keyword, REASON_NO_CPT_EXISTS, NEVER_CODED[keyword])

    if keyword in AXIS_NOT_SPOKEN:
        return NoProcedureCode(keyword, REASON_AXIS_NOT_SPOKEN, AXIS_NOT_SPOKEN[keyword])

    rule = KEYWORD_RULES.get(keyword)
    if rule is None:
        # A keyword the detector fires on that this table has never heard of.
        # Unmapped rather than undetermined: adding a keyword to
        # track_b_rag.keywords without a rule here lands exactly here.
        return NoProcedureCode(
            keyword,
            REASON_QUALIFIER_UNMAPPED,
            "no code rule is defined for this keyword",
        )

    anchor = mention.excerpt.find(mention.matched_text)
    qualifier = _best_qualifier(rule, mention.excerpt, max(anchor, 0))
    if qualifier is None:
        qualifier = rule.default_qualifier
    if qualifier is None:
        return NoProcedureCode(
            keyword,
            REASON_QUALIFIER_NOT_STATED,
            f"no {rule.axis} was stated, and it is what selects the code",
        )

    code = rule.codes.get(qualifier)
    if code is None:
        return NoProcedureCode(
            keyword,
            REASON_QUALIFIER_UNMAPPED,
            f"the {rule.axis} named has no code in the table yet",
        )
    return code
