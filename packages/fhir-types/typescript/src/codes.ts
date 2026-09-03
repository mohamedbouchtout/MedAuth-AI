/**
 * Closed FHIR R4 value sets, mirroring `src/fhir_types/codes.py`.
 *
 * Only value sets with a *required* binding appear here — the ones where FHIR
 * fixes the legal codes. Extensible bindings stay `CodeableConcept`, because
 * constraining those would reject valid payloads from real EHRs.
 *
 * Every alias here has a same-named `Literal` in codes.py, and
 * `tests/unit/test_typescript_parity.py` compares them member for member. Adding
 * a code on one side only fails CI.
 */

/** Patient.gender — https://hl7.org/fhir/R4/valueset-administrative-gender.html */
export type AdministrativeGender = 'male' | 'female' | 'other' | 'unknown';

/** Encounter.status — https://hl7.org/fhir/R4/valueset-encounter-status.html */
export type EncounterStatus =
  | 'planned'
  | 'arrived'
  | 'triaged'
  | 'in-progress'
  | 'onleave'
  | 'finished'
  | 'cancelled'
  | 'entered-in-error'
  | 'unknown';

/** DocumentReference.status — the R4 `document-reference-status` value set. */
export type DocumentReferenceStatus = 'current' | 'superseded' | 'entered-in-error';

/** DocumentReference.docStatus — the underlying composition's lifecycle state. */
export type CompositionStatus = 'preliminary' | 'final' | 'amended' | 'entered-in-error';

/** Shared by Coverage.status and Claim.status — the R4 `fm-status` value set. */
export type FinancialResourceStatus = 'active' | 'cancelled' | 'draft' | 'entered-in-error';

/** Claim.use. Prior authorization bundles use `preauthorization`. */
export type ClaimUse = 'claim' | 'preauthorization' | 'predetermination';

/** MedicationRequest.status — https://hl7.org/fhir/R4/valueset-medicationrequest-status.html */
export type MedicationRequestStatus =
  | 'active'
  | 'on-hold'
  | 'cancelled'
  | 'completed'
  | 'entered-in-error'
  | 'stopped'
  | 'draft'
  | 'unknown';

/** MedicationRequest.intent — https://hl7.org/fhir/R4/valueset-medicationrequest-intent.html */
export type MedicationRequestIntent =
  | 'proposal'
  | 'plan'
  | 'order'
  | 'original-order'
  | 'reflex-order'
  | 'filler-order'
  | 'instance-order'
  | 'option';

/** Shared by Encounter.priority-adjacent fields and MedicationRequest.priority. */
export type RequestPriority = 'routine' | 'urgent' | 'asap' | 'stat';

/** Identifier.use — https://hl7.org/fhir/R4/valueset-identifier-use.html */
export type IdentifierUse = 'usual' | 'official' | 'temp' | 'secondary' | 'old';

/** HumanName.use — https://hl7.org/fhir/R4/valueset-name-use.html */
export type NameUse =
  | 'usual'
  | 'official'
  | 'temp'
  | 'nickname'
  | 'anonymous'
  | 'old'
  | 'maiden';

/** ContactPoint.system — https://hl7.org/fhir/R4/valueset-contact-point-system.html */
export type ContactPointSystem = 'phone' | 'fax' | 'email' | 'pager' | 'url' | 'sms' | 'other';

/** ContactPoint.use — https://hl7.org/fhir/R4/valueset-contact-point-use.html */
export type ContactPointUse = 'home' | 'work' | 'temp' | 'old' | 'mobile';

/** Address.use — https://hl7.org/fhir/R4/valueset-address-use.html */
export type AddressUse = 'home' | 'work' | 'temp' | 'old' | 'billing';

/** Address.type — https://hl7.org/fhir/R4/valueset-address-type.html */
export type AddressType = 'postal' | 'physical' | 'both';

/**
 * Location.status — https://hl7.org/fhir/R4/valueset-location-status.html
 *
 * Whether the location record itself is in use. Not the same as
 * `EncounterLocationStatus` below, which is about one visit's stay at a place.
 */
export type LocationStatus = 'active' | 'suspended' | 'inactive';

/**
 * Location.mode — a specific place (`instance`) or a class of place (`kind`).
 *
 * Only an `instance` has a meaningful address. A `kind` describes something like
 * "a general practice room", so reading a state off one would be reading the
 * address of a template rather than of anywhere a service happened.
 */
export type LocationMode = 'instance' | 'kind';

/**
 * Encounter.location.status —
 * https://hl7.org/fhir/R4/valueset-encounter-location-status.html
 *
 * `planned` is a place the patient was expected at and may never have reached,
 * which is why TASK-052b's site-of-care resolution does not treat it as where
 * the service took place.
 */
export type EncounterLocationStatus = 'planned' | 'active' | 'reserved' | 'completed';

/** Quantity.comparator — present only when the value is a bound, not a measurement. */
export type QuantityComparator = '<' | '<=' | '>=' | '>';

/**
 * Bundle.type — https://hl7.org/fhir/R4/valueset-bundle-type.html
 *
 * Da Vinci PAS uses `collection` for both the request and the response bundle.
 * The other members are legal R4 and this package mirrors R4, so they stay — a
 * profile's constraints are the caller's business, not this package's.
 */
export type BundleType =
  | 'document'
  | 'message'
  | 'transaction'
  | 'transaction-response'
  | 'batch'
  | 'batch-response'
  | 'history'
  | 'searchset'
  | 'collection';

/** Bundle.entry.search.mode — https://hl7.org/fhir/R4/valueset-search-entry-mode.html */
export type SearchEntryMode = 'match' | 'include' | 'outcome';

/** Bundle.entry.request.method — https://hl7.org/fhir/R4/valueset-http-verb.html */
export type HTTPVerb = 'GET' | 'HEAD' | 'POST' | 'PUT' | 'DELETE' | 'PATCH';

/**
 * ClaimResponse.outcome — https://hl7.org/fhir/R4/valueset-remittance-outcome.html
 *
 * Whether the payer *processed* the request, not whether it approved it. A prior
 * authorization that was fully considered and denied is `complete`; the decision
 * itself is in `ClaimResponse.item.adjudication`. Reading `complete` as approval
 * is the mistake this comment exists to prevent.
 */
export type RemittanceOutcome = 'queued' | 'complete' | 'error' | 'partial';

/** ClaimResponse.processNote.type — https://hl7.org/fhir/R4/valueset-note-type.html */
export type NoteType = 'display' | 'print' | 'printoper';
