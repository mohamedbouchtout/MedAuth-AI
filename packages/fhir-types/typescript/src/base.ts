/**
 * Shared resource header, mirroring `src/fhir_types/base.py`.
 *
 * Two conventions carry over from the Python side and apply to every file here:
 *
 * - **Property names are the FHIR wire names**, camelCase, exactly as a server
 *   sends them. The Python models use snake_case internally and alias to these;
 *   there is no such split here, so what you see is what goes over the wire.
 * - **Everything is `readonly`.** The Pydantic models are frozen. A resource read
 *   from an EHR is a snapshot, not a mutable buffer — build a changed copy with a
 *   spread rather than assigning into one.
 *
 * Unlike the Python models these interfaces do not carry unmodelled elements in a
 * typed way; TypeScript structural typing simply ignores extra properties present
 * at runtime, which is the same practical outcome as `extra="allow"`.
 *
 * PHI: every resource in this package holds patient data. Do not log one, and do
 * not put one in an error message or a client-side analytics event.
 *
 * @remarks
 * Formatting here is load-bearing. `tests/unit/test_typescript_parity.py` parses
 * these files with a regex: one property per line, no inline object literal types,
 * and a closing brace on its own line. Give a nested shape its own interface.
 */

/** Server-maintained resource metadata. */
export interface Meta {
  /** Server-assigned version, used for optimistic locking on write. */
  readonly versionId?: string;
  /** When the resource was last changed, as an `instant`. */
  readonly lastUpdated?: string;
  /** URI identifying where the resource came from. */
  readonly source?: string;
  /** Profile URIs the resource claims to conform to, e.g. US Core. */
  readonly profile?: readonly string[];
}

/**
 * Common header shared by every resource type.
 *
 * `resourceType` is declared on each resource interface rather than here, so the
 * resources form a discriminated union narrowed by that one property.
 */
export interface DomainResource {
  /** Server-assigned logical id, unique within the FHIR server. */
  readonly id?: string;
  /** Server-maintained metadata. */
  readonly meta?: Meta;
  /** URI of a ruleset the content is only safe to read under. */
  readonly implicitRules?: string;
  /** Base language of the resource, as a BCP-47 tag. */
  readonly language?: string;
}
