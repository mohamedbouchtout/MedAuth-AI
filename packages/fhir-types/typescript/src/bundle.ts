/**
 * The FHIR R4 Bundle resource, mirroring `src/fhir_types/bundle.py`.
 *
 * Da Vinci PAS moves prior authorizations as bundles, not as bare Claims:
 * `Claim/$submit` takes a `Bundle` on `profile-pas-request-bundle` — a single
 * Claim plus every resource it references — and answers with a `Bundle` on
 * `profile-pas-response-bundle`, a `ClaimResponse` plus its referenced resources.
 *
 * `BundleEntry.resource` is `AnyResourceOrUnknown`, not `AnyResource`. A bundle
 * from outside carries referenced resources of types this package does not model
 * — `Practitioner`, `PractitionerRole`, `Task`, `OperationOutcome` — and the
 * Python side keeps those intact as an `UnknownResource` rather than raising.
 * Narrow on `resourceType` before reading anything off an entry.
 *
 * @remarks
 * Formatting here is load-bearing — see `base.ts`.
 */

import type { DomainResource } from './base.js';
import type { BundleType, HTTPVerb, SearchEntryMode } from './codes.js';
import type { Identifier } from './datatypes.js';
import type { AnyResourceOrUnknown } from './index.js';

/** A named link relevant to the bundle or to one entry. */
export interface BundleLink {
  /** The IANA relation name — `self`, `next`, `previous`. Required by FHIR. */
  readonly relation: string;
  /** The link's target. Required by FHIR. */
  readonly url: string;
}

/** Why an entry is in a search result. Prohibited by PAS; modelled anyway. */
export interface BundleEntrySearch {
  /** Whether the entry matched the search or was pulled in by `_include`. */
  readonly mode?: SearchEntryMode;
  /** Search relevance ranking, 0 to 1. */
  readonly score?: number;
}

/** The transaction or batch operation an entry represents. */
export interface BundleEntryRequest {
  /** The HTTP verb for this entry's operation. Required by FHIR. */
  readonly method: HTTPVerb;
  /** The request URL, relative to the server's base. Required by FHIR. */
  readonly url: string;
  /** `ETag`-based precondition for a conditional read. */
  readonly ifNoneMatch?: string;
  /** Timestamp precondition for a conditional read, as an `instant`. */
  readonly ifModifiedSince?: string;
  /** `ETag`-based precondition for a conditional update. */
  readonly ifMatch?: string;
  /** Search string for a conditional create. */
  readonly ifNoneExist?: string;
}

/** The server's outcome for one transaction or batch entry. */
export interface BundleEntryResponse {
  /** The HTTP status line for this entry. Required by FHIR. */
  readonly status: string;
  /** Where a created resource can be read from. */
  readonly location?: string;
  /** The created or updated resource's version, for optimistic locking. */
  readonly etag?: string;
  /** When the resource was changed, as an `instant`. */
  readonly lastModified?: string;
  /** An `OperationOutcome` carrying detail about the operation. */
  readonly outcome?: AnyResourceOrUnknown;
}

/** One resource carried by a bundle, with the metadata that frames it. */
export interface BundleEntry {
  /**
   * Absolute URL identifying the resource, and what intra-bundle references
   * resolve against. A PAS bundle's Claim points at its Patient and Coverage
   * entries through this, so dropping it breaks the request in a way the payer
   * reports as a missing resource rather than a bad link.
   */
  readonly fullUrl?: string;
  /** The resource itself. An unmodelled type stays open — see this file's header. */
  readonly resource?: AnyResourceOrUnknown;
  /** Search metadata. Prohibited by PAS. */
  readonly search?: BundleEntrySearch;
  /** Transaction or batch operation. Prohibited by PAS. */
  readonly request?: BundleEntryRequest;
  /** Transaction or batch outcome. Prohibited by PAS. */
  readonly response?: BundleEntryResponse;
  /** Links relevant to this entry alone. */
  readonly link?: readonly BundleLink[];
}

/** A container for a collection of resources. */
export interface Bundle extends DomainResource {
  readonly resourceType: 'Bundle';
  /** Persistent identifier for the bundle, assigned by its author. */
  readonly identifier?: Identifier;
  /** What the bundle is for. PAS uses `collection`. Required by FHIR. */
  readonly type: BundleType;
  /** When the bundle was assembled, as an `instant`. */
  readonly timestamp?: string;
  /**
   * Number of matches in a `searchset` or `history`. Not a count of entries — an
   * `_include`d resource is an entry and not a match.
   */
  readonly total?: number;
  /** Links relevant to the bundle as a whole, e.g. paging. */
  readonly link?: readonly BundleLink[];
  /** The resources carried, each with its own framing metadata. */
  readonly entry?: readonly BundleEntry[];
}
