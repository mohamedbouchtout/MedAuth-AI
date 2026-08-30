/**
 * Reading one nudge off the wire.
 *
 * The shape is fixed in CLAUDE.md, "The nudge payload — one shape", and this app
 * is one of five things that agree on it: track-b-rag publishes it (TASK-040),
 * nudge-service relays it verbatim (TASK-041), and both frontends render it.
 * Nothing here may invent a field or repair a missing one.
 *
 * **`cpt_code` is nullable and always has been.** TASK-040 never emits null, but
 * TASK-044 nudges on a keyword that resolved no CPT code, and a component typing
 * this as `string` would break on the first one. The nullability is in the
 * contract rather than in the emitter, so it is honoured here before that task
 * exists.
 *
 * **A payload this cannot read is dropped, silently.** The relay forwards
 * anything UTF-8 without parsing it — deliberately, so an emitter bug does not
 * become silence at the bedside — which means a malformed message can arrive
 * here. Rendering a half-empty banner would be worse than dropping it: a
 * provider cannot act on an alert that names no procedure. Nothing about the
 * drop is logged, because the payload is PHI.
 *
 * `haptic` is parsed and deliberately unused on web: it is TASK-043's escalation
 * on a device that can buzz, and it is *not* a synonym for high risk. Keeping it
 * in the type stops a later reader concluding the field is optional.
 */

/** How likely the payer is to deny the order as documented. Drives the banner. */
export type DenialRisk = 'low' | 'medium' | 'high';

export interface Nudge {
  type: string;
  nudgeId: string;
  procedure: string;
  /** Null when the procedure resolved no CPT code — TASK-044. */
  cptCode: string | null;
  message: string;
  /**
   * The payer criteria not yet evidenced.
   *
   * Empty is meaningful and is not the same as "nothing is missing": on the safe
   * fallback answer it means the criteria are *unknown*. The component says so
   * rather than implying the documentation is complete.
   */
  missingCriteria: string[];
  denialRisk: DenialRisk;
  /** Whether a device should buzz. Mobile acts on this; web reads and ignores it. */
  haptic: boolean;
}

const DENIAL_RISKS: readonly string[] = ['low', 'medium', 'high'];

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === 'string');
}

/**
 * Parse one WebSocket frame into a nudge, or null if it is not one.
 *
 * Null covers every malformed shape — not JSON, not an object, a missing or
 * wrong-typed field, a `denial_risk` outside the vocabulary — because the caller
 * does the same thing in all of them, and telling them apart would mean
 * describing the contents of a message that carries PHI.
 */
export function parseNudge(frame: string): Nudge | null {
  let value: unknown;
  try {
    value = JSON.parse(frame);
  } catch {
    return null;
  }

  if (typeof value !== 'object' || value === null) {
    return null;
  }

  const {
    type,
    nudge_id: nudgeId,
    procedure,
    cpt_code: cptCode,
    message,
    missing_criteria: missingCriteria,
    denial_risk: denialRisk,
    haptic,
  } = value as Record<string, unknown>;

  if (
    typeof type !== 'string' ||
    typeof nudgeId !== 'string' ||
    typeof procedure !== 'string' ||
    typeof message !== 'string' ||
    typeof haptic !== 'boolean' ||
    !isStringArray(missingCriteria) ||
    typeof denialRisk !== 'string' ||
    !DENIAL_RISKS.includes(denialRisk)
  ) {
    return null;
  }

  // Null and absent are both "no code". A string of any other type is a
  // malformed payload rather than a code this app should try to render.
  if (cptCode !== null && cptCode !== undefined && typeof cptCode !== 'string') {
    return null;
  }

  return {
    type,
    nudgeId,
    procedure,
    cptCode: typeof cptCode === 'string' ? cptCode : null,
    message,
    missingCriteria,
    denialRisk: denialRisk as DenialRisk,
    haptic,
  };
}
