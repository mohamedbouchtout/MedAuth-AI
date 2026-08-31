/**
 * The FHIR R4 Encounter resource, mirroring `src/fhir_types/encounter.py`.
 *
 * The encounter is the anchor for everything this platform produces: the ambient
 * audio session, the generated SOAP note, and any prior authorization that comes
 * out of it all hang off one Encounter id.
 */

import type { DomainResource } from './base.js';
import type { EncounterLocationStatus, EncounterStatus } from './codes.js';
import type { CodeableConcept, Coding, Identifier, Period, Reference } from './datatypes.js';

/** A practitioner or other person involved in the encounter. */
export interface EncounterParticipant {
  /** The role the participant played. */
  readonly type?: readonly CodeableConcept[];
  /** The span of the encounter they were present for. */
  readonly period?: Period;
  /** Reference to the Practitioner, PractitionerRole or RelatedPerson. */
  readonly individual?: Reference;
}

/** A diagnosis relevant to the encounter. */
export interface EncounterDiagnosis {
  /** Reference to the Condition or Procedure. Required by FHIR. */
  readonly condition: Reference;
  /** Role the diagnosis played — admission, discharge, billing, and so on. */
  readonly use?: CodeableConcept;
  /** Ranking among the encounter's diagnoses, 1 being primary. */
  readonly rank?: number;
}

/**
 * A place the patient was during the encounter.
 *
 * This is the primary source of the encounter's site of care (TASK-052b):
 * `location` is resolved to a `Location` resource and its `address.state`
 * becomes the `state` segment of the RAG cache key. The payer policies this
 * platform reads scope themselves that way — CMS's Medicare Coverage Database
 * says to search by "the state where the service took place" — and no document
 * in the seed corpus points at the patient's residence.
 *
 * A visit can list several: admitted here, moved to a ward, seen in a theatre.
 * `status` is what separates the ones the patient actually reached from the ones
 * that were only ever planned.
 */
export interface EncounterLocation {
  /** Reference to the `Location`. Required by FHIR. */
  readonly location: Reference;
  /** Whether the patient is or was present at this location. */
  readonly status?: EncounterLocationStatus;
  /** The kind of place — a bed, a room, a building. */
  readonly physicalType?: CodeableConcept;
  /** When the patient was at this location. */
  readonly period?: Period;
}

/**
 * An interaction between a patient and one or more healthcare providers.
 *
 * The `class` property keeps its FHIR name here. On the Python side it is
 * `encounter_class`, because `class` is a reserved word there; it aliases back to
 * `class` on the wire, so the two shapes match.
 */
export interface Encounter extends DomainResource {
  readonly resourceType: 'Encounter';
  /** Business identifiers for the encounter. */
  readonly identifier?: readonly Identifier[];
  /** Where the encounter is in its lifecycle. Required by FHIR. */
  readonly status: EncounterStatus;
  /** Classification — ambulatory, inpatient, virtual. */
  readonly class?: Coding;
  /** Specific kind of encounter. */
  readonly type?: readonly CodeableConcept[];
  /** Broad category of service performed. */
  readonly serviceType?: CodeableConcept;
  /** Urgency of the encounter. */
  readonly priority?: CodeableConcept;
  /** The patient present at the encounter. */
  readonly subject?: Reference;
  /** Practitioners and others involved. */
  readonly participant?: readonly EncounterParticipant[];
  /** Start and end of the encounter. */
  readonly period?: Period;
  /** Coded reason the encounter took place. */
  readonly reasonCode?: readonly CodeableConcept[];
  /** Reason expressed as a reference to another resource. */
  readonly reasonReference?: readonly Reference[];
  /** Diagnoses relevant to this encounter. */
  readonly diagnosis?: readonly EncounterDiagnosis[];
  /** Places the patient was during the encounter. The site of care TASK-052b
   * reads `state` from. */
  readonly location?: readonly EncounterLocation[];
  /** Organization responsible for the encounter. The fallback site-of-care
   * source when no `location` resolves. */
  readonly serviceProvider?: Reference;
  /** Encounter this one is a part of. */
  readonly partOf?: Reference;
}
