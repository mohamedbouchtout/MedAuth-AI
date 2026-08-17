/**
 * The FHIR R4 DocumentReference resource, mirroring
 * `src/fhir_types/document_reference.py`.
 *
 * This is how a generated SOAP note gets back into the EHR. The note body travels
 * base64-encoded in `content[].attachment.data` and is PHI in full.
 */

import type { DomainResource } from './base.js';
import type { CompositionStatus, DocumentReferenceStatus } from './codes.js';
import type {
  Attachment,
  CodeableConcept,
  Coding,
  Identifier,
  Period,
  Reference,
} from './datatypes.js';

/** The document itself, plus how it is encoded. */
export interface DocumentReferenceContent {
  /** The document content or a URL to it. Required by FHIR. */
  readonly attachment: Attachment;
  /** Format or profile the content conforms to beyond its MIME type. */
  readonly format?: Coding;
}

/** The clinical context the document was produced in. */
export interface DocumentReferenceContext {
  /** Encounters the document is about — the ambient session's Encounter goes
   * here, which is what links a note back to its recording. */
  readonly encounter?: readonly Reference[];
  /** Clinical acts the document describes. */
  readonly event?: readonly CodeableConcept[];
  /** Time of service the document covers. */
  readonly period?: Period;
  /** Kind of facility where the patient was seen. */
  readonly facilityType?: CodeableConcept;
  /** Clinical specialty of the practice, e.g. orthopedics. */
  readonly practiceSetting?: CodeableConcept;
  /** The patient demographics as of authoring. */
  readonly sourcePatientInfo?: Reference;
  /** Other resources associated with the document. */
  readonly related?: readonly Reference[];
}

/** A reference to a clinical document, with the document attached or linked. */
export interface DocumentReference extends DomainResource {
  readonly resourceType: 'DocumentReference';
  /** Version-specific identifier for the document. */
  readonly masterIdentifier?: Identifier;
  /** Other business identifiers. */
  readonly identifier?: readonly Identifier[];
  /** Whether the reference is current. Required by FHIR. */
  readonly status: DocumentReferenceStatus;
  /** Lifecycle state of the underlying document. A SOAP note pending physician
   * sign-off is `preliminary`; `final` after attestation. */
  readonly docStatus?: CompositionStatus;
  /** Kind of document, e.g. a LOINC progress note code. */
  readonly type?: CodeableConcept;
  /** Broad categorization of the document. */
  readonly category?: readonly CodeableConcept[];
  /** The patient the document is about. */
  readonly subject?: Reference;
  /** When the reference was created, as an `instant`. */
  readonly date?: string;
  /** Who produced the document. */
  readonly author?: readonly Reference[];
  /** Who attested to its accuracy. */
  readonly authenticator?: Reference;
  /** Organization maintaining the document. */
  readonly custodian?: Reference;
  /** Human-readable description. Must not restate note content. */
  readonly description?: string;
  /** The document itself. Required by FHIR, at least one entry. */
  readonly content: readonly DocumentReferenceContent[];
  /** Clinical context of the document. */
  readonly context?: DocumentReferenceContext;
}
