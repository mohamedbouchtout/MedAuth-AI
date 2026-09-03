/**
 * FHIR R4 (4.0.1) types shared by `apps/web` and `apps/mobile`.
 *
 * These mirror the Pydantic models in `packages/fhir-types/src/fhir_types/`. The
 * Python side is authoritative: it is what the backend validates against, and
 * `tests/unit/test_typescript_parity.py` fails CI if the two drift apart. Change
 * a model there first, then reflect it here.
 *
 * Both apps reach FHIR through fhirclient.js, which returns `any`. Narrow at that
 * boundary and the rest of the app gets real types:
 *
 * ```ts
 * import type { Patient } from '@medauth/fhir-types';
 *
 * const patient = (await client.patient.read()) as Patient;
 * ```
 *
 * That cast is unchecked — it asserts the server sent what it claims. The backend
 * validates the same payloads with Pydantic, so a shape mismatch surfaces there
 * rather than silently corrupting a screen. Do not add runtime validation here to
 * compensate: this package is types plus one version constant, and keeping it that
 * way is what lets it be imported from anywhere without pulling in a dependency.
 *
 * Named exports only, per CLAUDE.md — there is no default export.
 */

export type { DomainResource, Meta } from './base.js';
export type {
  Bundle,
  BundleEntry,
  BundleEntryRequest,
  BundleEntryResponse,
  BundleEntrySearch,
  BundleLink,
} from './bundle.js';
export type {
  AddressType,
  AddressUse,
  AdministrativeGender,
  BundleType,
  ClaimUse,
  CompositionStatus,
  ContactPointSystem,
  ContactPointUse,
  DocumentReferenceStatus,
  EncounterLocationStatus,
  EncounterStatus,
  FinancialResourceStatus,
  HTTPVerb,
  IdentifierUse,
  LocationMode,
  LocationStatus,
  MedicationRequestIntent,
  MedicationRequestStatus,
  NameUse,
  NoteType,
  QuantityComparator,
  RemittanceOutcome,
  RequestPriority,
  SearchEntryMode,
} from './codes.js';
export type {
  Address,
  Annotation,
  Attachment,
  CodeableConcept,
  Coding,
  ContactPoint,
  HumanName,
  Identifier,
  Money,
  Period,
  Quantity,
  Reference,
} from './datatypes.js';
export type {
  Claim,
  ClaimCareTeam,
  ClaimDiagnosis,
  ClaimInsurance,
  ClaimItem,
  ClaimProcedure,
  ClaimSupportingInfo,
} from './claim.js';
export type {
  ClaimResponse,
  ClaimResponseAdjudication,
  ClaimResponseError,
  ClaimResponseInsurance,
  ClaimResponseItem,
  ClaimResponseProcessNote,
  ClaimResponseTotal,
} from './claimResponse.js';
export type { Condition } from './condition.js';
export type { Coverage, CoverageClass } from './coverage.js';
export type {
  DocumentReference,
  DocumentReferenceContent,
  DocumentReferenceContext,
} from './documentReference.js';
export type {
  Encounter,
  EncounterDiagnosis,
  EncounterLocation,
  EncounterParticipant,
} from './encounter.js';
export type { Location, LocationPosition } from './location.js';
export type { Organization } from './organization.js';
export type { Dosage, DosageDoseAndRate, MedicationRequest } from './medicationRequest.js';
export type { Patient, PatientCommunication } from './patient.js';

import type { Bundle } from './bundle.js';
import type { Claim } from './claim.js';
import type { ClaimResponse } from './claimResponse.js';
import type { Condition } from './condition.js';
import type { Coverage } from './coverage.js';
import type { DocumentReference } from './documentReference.js';
import type { Encounter } from './encounter.js';
import type { Location } from './location.js';
import type { MedicationRequest } from './medicationRequest.js';
import type { Organization } from './organization.js';
import type { Patient } from './patient.js';

/** The FHIR release these types target. See CLAUDE.md — R4, not R4B or R5. */
export const FHIR_VERSION = '4.0.1';

/**
 * Any resource this package models, discriminated by `resourceType`.
 *
 * Narrowing on that one property gives the right shape without a cast:
 *
 * ```ts
 * function subjectOf(resource: AnyResource): string | undefined {
 *   return resource.resourceType === 'Patient' ? resource.id : undefined;
 * }
 * ```
 */
export type AnyResource =
  | Bundle
  | Claim
  | ClaimResponse
  | Condition
  | Coverage
  | DocumentReference
  | Encounter
  | Location
  | MedicationRequest
  | Organization
  | Patient;

/**
 * A resource of a type this package does not model, kept open rather than narrowed.
 *
 * Mirrors `UnknownResource` in `src/fhir_types/__init__.py`. It is a type alias
 * rather than an interface for the same reason the Python model declares no
 * fields: there are no elements to mirror, only the promise that `resourceType`
 * is there and everything else came through untouched.
 */
export type UnknownResource = {
  readonly resourceType: string;
} & Readonly<Record<string, unknown>>;

/**
 * `AnyResource`, widened to tolerate a resource type this package does not model.
 *
 * This is what `Bundle.entry.resource` is, and it is the right type for anything
 * reading a bundle that came from outside — a payer's PAS response above all. A
 * PAS response carries referenced resources conformantly including `Practitioner`,
 * `PractitionerRole` and `Task`, none of which are modelled here, so narrowing to
 * `AnyResource` would reject valid payer responses.
 *
 * Narrow with a `resourceType` check before reading a modelled element:
 *
 * ```ts
 * const claimResponse = bundle.entry
 *   ?.map((entry) => entry.resource)
 *   .find((resource) => resource?.resourceType === 'ClaimResponse') as ClaimResponse | undefined;
 * ```
 */
export type AnyResourceOrUnknown = AnyResource | UnknownResource;
