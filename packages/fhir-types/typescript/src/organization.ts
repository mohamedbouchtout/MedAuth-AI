/**
 * The FHIR R4 Organization resource, mirroring `src/fhir_types/organization.py`.
 *
 * Modelled as the fallback half of TASK-052b's site-of-care rule. An
 * `Encounter.serviceProvider` names the organization that performed the service,
 * so its address answers "where did this take place" whenever the encounter
 * carries no resolvable `Location`.
 *
 * It is a fallback rather than an equal source, and the order is not arbitrary:
 * an organization can span states, so its address is a coarser answer than the
 * specific room a patient was seen in. When neither resolves, `state` stays null
 * — the patient's own address is *not* a third fallback, because the documents
 * say the site of care and a residence is a different fact that happens to be
 * the same value most of the time.
 */

import type { DomainResource } from './base.js';
import type { Address, CodeableConcept, ContactPoint, Identifier, Reference } from './datatypes.js';

/** A grouping of people or organizations with a common purpose. */
export interface Organization extends DomainResource {
  readonly resourceType: 'Organization';
  /** Business identifiers — an NPI, a tax id. */
  readonly identifier?: readonly Identifier[];
  /** Whether the organization's record is in active use. */
  readonly active?: boolean;
  /** The kind of organization, e.g. a healthcare provider. */
  readonly type?: readonly CodeableConcept[];
  /** The organization's name. */
  readonly name?: string;
  /** Other names it has been known by. */
  readonly alias?: readonly string[];
  /** Contact details. */
  readonly telecom?: readonly ContactPoint[];
  /** Postal addresses. A *list* in R4, unlike `Location.address`. Not every entry
   * describes where care happens — a billing address routinely sits in another
   * state — so the site-of-care rule picks among them rather than taking the
   * first. That choice lives with the code that applies it, in `fhir-integration`. */
  readonly address?: readonly Address[];
  /** The organization this one belongs to. */
  readonly partOf?: Reference;
}
