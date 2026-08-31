/**
 * FHIR R4 general-purpose datatypes, mirroring `src/fhir_types/datatypes.py`.
 *
 * Dates and times are `string`, not `Date`. FHIR's `date` and `dateTime` permit
 * reduced precision — `"2024"` and `"2024-03"` are both legal birth dates and real
 * EHR data contains them. `new Date("2024-03")` silently invents a day, so the
 * values stay as the server sent them and a caller parses at the point of use.
 */

import type {
  AddressType,
  AddressUse,
  ContactPointSystem,
  ContactPointUse,
  IdentifierUse,
  NameUse,
  QuantityComparator,
} from './codes.js';

/** A single code drawn from a code system. */
export interface Coding {
  /** URI of the code system, e.g. `http://snomed.info/sct`. */
  readonly system?: string;
  /** Version of the code system, when the code's meaning depends on it. */
  readonly version?: string;
  /** The symbol itself, in syntax defined by the system. */
  readonly code?: string;
  /** Human-readable label for the code, as the system defines it. */
  readonly display?: string;
  /** True when a human picked this code directly. */
  readonly userSelected?: boolean;
}

/**
 * A concept expressed as codes from one or more systems, plus free text.
 *
 * Most clinically meaningful fields are this rather than a bare `Coding`: the same
 * diagnosis routinely arrives coded in both ICD-10 and SNOMED, and payers differ
 * on which they accept.
 */
export interface CodeableConcept {
  /** Equivalent codings of the same concept across systems. */
  readonly coding?: readonly Coding[];
  /** The concept as the source presented it to a human. */
  readonly text?: string;
}

/** A time range bounded by start and end. */
export interface Period {
  /** Inclusive start, as a `dateTime` string. */
  readonly start?: string;
  /** Inclusive end. Absent means the period has no known end. */
  readonly end?: string;
}

/** A business identifier — an MRN, a policy number, a claim number. */
export interface Identifier {
  /** Role this identifier plays for its owner. */
  readonly use?: IdentifierUse;
  /** Coded description of the identifier's kind. */
  readonly type?: CodeableConcept;
  /** Namespace the value is unique within. */
  readonly system?: string;
  /** The identifier itself. Frequently PHI (an MRN or member id). */
  readonly value?: string;
  /** When the identifier was or is valid. */
  readonly period?: Period;
  /** Organization that issued it. */
  readonly assigner?: Reference;
}

/** A pointer from one resource to another. */
export interface Reference {
  /** Relative or absolute URL, e.g. `Patient/1234`. */
  readonly reference?: string;
  /** Resource type being referred to, when `reference` is absent. */
  readonly type?: string;
  /** Business identifier for the target, when it has no URL. */
  readonly identifier?: Identifier;
  /** Human-readable label for the target. */
  readonly display?: string;
}

/** A name of a human, with the parts kept separate. Always PHI. */
export interface HumanName {
  /** Role this name plays — official, nickname, maiden, and so on. */
  readonly use?: NameUse;
  /** The full name as it should be displayed. */
  readonly text?: string;
  /** Family name (surname). */
  readonly family?: string;
  /** Given and middle names, in order. */
  readonly given?: readonly string[];
  /** Titles preceding the name, e.g. `Dr`. */
  readonly prefix?: readonly string[];
  /** Qualifiers following the name, e.g. `Jr`, `MD`. */
  readonly suffix?: readonly string[];
  /** When this name was or is in use. */
  readonly period?: Period;
}

/** A phone number, email address or other contact detail. Always PHI. */
export interface ContactPoint {
  /** Which kind of contact channel this is. */
  readonly system?: ContactPointSystem;
  /** The number or address itself. */
  readonly value?: string;
  /** The context this channel is used in. */
  readonly use?: ContactPointUse;
  /** Preference order, 1 being most preferred. */
  readonly rank?: number;
  /** When this channel was or is in use. */
  readonly period?: Period;
}

/** A postal address. Always PHI. */
export interface Address {
  /** The purpose this address serves. */
  readonly use?: AddressUse;
  /** Whether the address is postal, physical, or both. */
  readonly type?: AddressType;
  /** The address as a single displayable string. */
  readonly text?: string;
  /** Street lines, in order. */
  readonly line?: readonly string[];
  /** City or town. */
  readonly city?: string;
  /** County or district. */
  readonly district?: string;
  /** State or province. **Not** the source of the `state` component of the RAG
   * cache key, whatever resource this address hangs off. An earlier version of
   * this comment said it was, written before anyone read what the policy
   * documents say about their own applicability. They say the site of care —
   * CMS's Medicare Coverage Database instructs the reader to select "the state
   * where the service took place" — so the key's `state` comes from the
   * encounter's Location/Organization address, never from a `Patient.address`.
   * See TASK-052b, "Where `state` comes from". */
  readonly state?: string;
  /** Postal or ZIP code. */
  readonly postalCode?: string;
  /** Country, as a name or ISO 3166 code. */
  readonly country?: string;
  /** When this address was or is in use. */
  readonly period?: Period;
}

/** Content referred to in-line or by URL. */
export interface Attachment {
  /** MIME type of the content, including character encoding. */
  readonly contentType?: string;
  /** Language of the content, as a BCP-47 tag. */
  readonly language?: string;
  /** Base64 of the content itself. On a DocumentReference this is the note body —
   * PHI, and never safe to log or put in an error message. */
  readonly data?: string;
  /** Location the content can be retrieved from instead. */
  readonly url?: string;
  /** Size in bytes of the decoded content. */
  readonly size?: number;
  /** Label for the attachment. */
  readonly title?: string;
  /** When the attachment was first created, as a `dateTime`. */
  readonly creation?: string;
}

/** A measured or counted amount with a unit. */
export interface Quantity {
  /** The numeric value. */
  readonly value?: number;
  /** Set when the value is a bound rather than a measurement. */
  readonly comparator?: QuantityComparator;
  /** Human-readable unit. */
  readonly unit?: string;
  /** URI of the unit system, usually UCUM. */
  readonly system?: string;
  /** Machine-readable form of the unit. */
  readonly code?: string;
}

/** An amount of currency. */
export interface Money {
  /** The numeric amount. */
  readonly value?: number;
  /** ISO 4217 currency code. */
  readonly currency?: string;
}

/**
 * A free-text note attached to a resource, with its author and time.
 *
 * `text` is clinician-authored prose and is PHI in every case this project
 * encounters it.
 */
export interface Annotation {
  /** The author, as a reference to a Practitioner or Patient. */
  readonly authorReference?: Reference;
  /** The author's name, when there is no resource for them. */
  readonly authorString?: string;
  /** When the note was made, as a `dateTime`. */
  readonly time?: string;
  /** The note itself. */
  readonly text: string;
}
