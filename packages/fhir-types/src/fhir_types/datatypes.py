"""FHIR R4 general-purpose datatypes.

These are the reusable elements the seven modelled resources are built from. They
are not resources: none carries a ``resourceType`` and none can be read or written
against a FHIR server on its own.

**Dates and times are ``str``, not ``date``/``datetime``.** FHIR's ``date`` and
``dateTime`` permit reduced precision — ``"2024"`` and ``"2024-03"`` are both legal
values for a birth date, and real EHR data contains them. ``datetime.date`` cannot
represent either, so parsing into it would reject valid payloads or silently invent
a day. The values stay as the server sent them; a caller that needs a real date
parses it at the point of use, where it can decide what a partial date means.
"""

from __future__ import annotations

from .base import FHIRBase
from .codes import (
    AddressType,
    AddressUse,
    ContactPointSystem,
    ContactPointUse,
    IdentifierUse,
    NameUse,
    QuantityComparator,
)


class Coding(FHIRBase):
    """A single code drawn from a code system.

    Attributes:
        system: URI of the code system, e.g. ``http://snomed.info/sct``.
        version: Version of the code system, when the code's meaning depends on it.
        code: The symbol itself, in syntax defined by the system.
        display: Human-readable label for the code, as the system defines it.
        user_selected: True when a human picked this code directly.
    """

    system: str | None = None
    version: str | None = None
    code: str | None = None
    display: str | None = None
    user_selected: bool | None = None


class CodeableConcept(FHIRBase):
    """A concept expressed as codes from one or more systems, plus free text.

    Most clinically meaningful fields are this rather than a bare ``Coding``: the
    same diagnosis routinely arrives coded in both ICD-10 and SNOMED, and payers
    differ on which they will accept.

    Attributes:
        coding: Equivalent codings of the same concept across systems.
        text: The concept as the source presented it to a human.
    """

    coding: list[Coding] | None = None
    text: str | None = None


class Period(FHIRBase):
    """A time range bounded by start and end.

    Attributes:
        start: Inclusive start, as a ``dateTime`` string.
        end: Inclusive end. Absent means the period has no known end.
    """

    start: str | None = None
    end: str | None = None


class Identifier(FHIRBase):
    """A business identifier — an MRN, a policy number, a claim number.

    Attributes:
        use: Role this identifier plays for its owner.
        type: Coded description of the identifier's kind.
        system: Namespace the value is unique within.
        value: The identifier itself. Frequently PHI (an MRN or member id).
        period: When the identifier was or is valid.
        assigner: Organization that issued it.
    """

    use: IdentifierUse | None = None
    type: CodeableConcept | None = None
    system: str | None = None
    value: str | None = None
    period: Period | None = None
    assigner: Reference | None = None


class Reference(FHIRBase):
    """A pointer from one resource to another.

    Attributes:
        reference: Relative or absolute URL, e.g. ``Patient/1234``.
        type: Resource type being referred to, when ``reference`` is absent.
        identifier: Business identifier for the target, when it has no URL.
        display: Human-readable label for the target.
    """

    reference: str | None = None
    type: str | None = None
    identifier: Identifier | None = None
    display: str | None = None


class HumanName(FHIRBase):
    """A name of a human, with the parts kept separate. Always PHI.

    Attributes:
        use: Role this name plays — official, nickname, maiden, and so on.
        text: The full name as it should be displayed.
        family: Family name (surname).
        given: Given and middle names, in order.
        prefix: Titles preceding the name, e.g. ``Dr``.
        suffix: Qualifiers following the name, e.g. ``Jr``, ``MD``.
        period: When this name was or is in use.
    """

    use: NameUse | None = None
    text: str | None = None
    family: str | None = None
    given: list[str] | None = None
    prefix: list[str] | None = None
    suffix: list[str] | None = None
    period: Period | None = None


class ContactPoint(FHIRBase):
    """A phone number, email address or other contact detail. Always PHI.

    Attributes:
        system: Which kind of contact channel this is.
        value: The number or address itself.
        use: The context this channel is used in.
        rank: Preference order, 1 being most preferred.
        period: When this channel was or is in use.
    """

    system: ContactPointSystem | None = None
    value: str | None = None
    use: ContactPointUse | None = None
    rank: int | None = None
    period: Period | None = None


class Address(FHIRBase):
    """A postal address. Always PHI.

    Attributes:
        use: The purpose this address serves.
        type: Whether the address is postal, physical, or both.
        text: The address as a single displayable string.
        line: Street lines, in order.
        city: City or town.
        district: County or district.
        state: State or province — payer policies are state-scoped, so this drives
            the ``state`` component of the RAG cache key described in CLAUDE.md.
        postal_code: Postal or ZIP code.
        country: Country, as a name or ISO 3166 code.
        period: When this address was or is in use.
    """

    use: AddressUse | None = None
    type: AddressType | None = None
    text: str | None = None
    line: list[str] | None = None
    city: str | None = None
    district: str | None = None
    state: str | None = None
    postal_code: str | None = None
    country: str | None = None
    period: Period | None = None


class Attachment(FHIRBase):
    """Content referred to in-line or by URL.

    Attributes:
        content_type: MIME type of the content, including character encoding.
        language: Language of the content, as a BCP-47 tag.
        data: Base64 of the content itself. On a DocumentReference this is the note
            body — PHI, and never safe to log or put in an error message.
        url: Location the content can be retrieved from instead.
        size: Size in bytes of the decoded content.
        title: Label for the attachment.
        creation: When the attachment was first created, as a ``dateTime``.
    """

    content_type: str | None = None
    language: str | None = None
    data: str | None = None
    url: str | None = None
    size: int | None = None
    title: str | None = None
    creation: str | None = None


class Quantity(FHIRBase):
    """A measured or counted amount with a unit.

    Attributes:
        value: The numeric value.
        comparator: Set when the value is a bound rather than a measurement.
        unit: Human-readable unit.
        system: URI of the unit system, usually UCUM.
        code: Machine-readable form of the unit.
    """

    value: float | None = None
    comparator: QuantityComparator | None = None
    unit: str | None = None
    system: str | None = None
    code: str | None = None


class Money(FHIRBase):
    """An amount of currency.

    Attributes:
        value: The numeric amount.
        currency: ISO 4217 currency code.
    """

    value: float | None = None
    currency: str | None = None


class Annotation(FHIRBase):
    """A free-text note attached to a resource, with its author and time.

    ``text`` is clinician-authored prose and is PHI in every case this project
    encounters it.

    Attributes:
        author_reference: The author, as a reference to a Practitioner or Patient.
        author_string: The author's name, when there is no resource for them.
        time: When the note was made, as a ``dateTime``.
        text: The note itself.
    """

    author_reference: Reference | None = None
    author_string: str | None = None
    time: str | None = None
    text: str


# Identifier.assigner forward-references Reference, which in turn holds an
# Identifier. The cycle is legal but has to be resolved once both exist.
Identifier.model_rebuild()
