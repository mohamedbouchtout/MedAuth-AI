/**
 * The FHIR R4 Location resource, mirroring `src/fhir_types/location.py`.
 *
 * Modelled for one reason: it is where an encounter physically happened, and the
 * site of care is what selects which payer policy applies. TASK-052b establishes
 * that from the documents themselves — CMS's Medicare Coverage Database says to
 * search by "the state where the service took place" — so `Location.address.state`
 * is the primary source of the `state` segment of
 * `rag:{payer}:{plan_type}:{state}:{cpt_code}`.
 *
 * `mode` matters more than it looks: a `kind` location describes a *class* of
 * place rather than a particular one, so its address, where it has one at all,
 * belongs to a template and not to anywhere a service was performed.
 */

import type { DomainResource } from './base.js';
import type { LocationMode, LocationStatus } from './codes.js';
import type {
  Address,
  CodeableConcept,
  ContactPoint,
  Identifier,
  Reference,
} from './datatypes.js';

/**
 * The location's absolute geographic position, in WGS84.
 *
 * Modelled because Synthea populates it on every generated facility. Nothing in
 * this platform reads it — the site-of-care rule works from the postal address,
 * which is the form a policy's own jurisdiction is published in.
 */
export interface LocationPosition {
  /** Degrees east of the prime meridian. Required by FHIR. */
  readonly longitude: number;
  /** Degrees north of the equator. Required by FHIR. */
  readonly latitude: number;
  /** Metres above the WGS84 ellipsoid. */
  readonly altitude?: number;
}

/** A physical place where care is delivered. */
export interface Location extends DomainResource {
  readonly resourceType: 'Location';
  /** Business identifiers for the place. */
  readonly identifier?: readonly Identifier[];
  /** Whether the location record is active. Required binding. */
  readonly status?: LocationStatus;
  /** The name the organization calls this place. */
  readonly name?: string;
  /** Other names it has been known by. */
  readonly alias?: readonly string[];
  /** Free-text description of the place. */
  readonly description?: string;
  /** Whether this is a specific place or a class of place. See the module comment. */
  readonly mode?: LocationMode;
  /** The kind of function performed at the location. */
  readonly type?: readonly CodeableConcept[];
  /** Contact details for the place. */
  readonly telecom?: readonly ContactPoint[];
  /** The postal address. The site-of-care address the `state` cache-key segment
   * is read from (TASK-052b), and singular in R4 — unlike `Organization.address`. */
  readonly address?: Address;
  /** Whether this is a building, a room, a vehicle and so on. */
  readonly physicalType?: CodeableConcept;
  /** Geographic coordinates. */
  readonly position?: LocationPosition;
  /** The organization responsible for the place. */
  readonly managingOrganization?: Reference;
  /** The location this one sits inside. */
  readonly partOf?: Reference;
}
