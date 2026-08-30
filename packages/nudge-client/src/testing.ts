/**
 * The wire-shape fixture builder, shared by every suite that fakes a nudge.
 *
 * **Why this ships from the package rather than being copied into each suite.**
 * It produces the payload in *snake_case*, exactly as track-b-rag publishes it
 * and nudge-service relays it — not this package's parsed `Nudge`. A test built
 * from the parsed type would check the components against the parser's output
 * instead of against what actually crosses the socket, which is the one thing
 * these tests exist to pin down.
 *
 * That makes the builder a second statement of the contract in CLAUDE.md's "The
 * nudge payload — one shape". One copy per suite would mean three of them —
 * this package's, `apps/web`'s and `apps/mobile`'s — drifting apart exactly as
 * the parsers would have, and a fixture that has drifted produces green tests
 * for a payload nobody sends.
 *
 * It is a `/testing` subpath rather than part of the main entry point so no
 * application bundle can reach a fixture by autocomplete, and it is excluded
 * from the coverage gate: it is test scaffolding, and counting it would measure
 * the suites' use of a helper rather than the code under test.
 */

/**
 * One nudge as track-b-rag publishes it, with any field overridable.
 *
 * The default is the coded, high-risk, escalating case. The cases worth naming
 * explicitly in a test are the ones that differ from it: `cpt_code: null`
 * (TASK-044), an empty `missing_criteria` (the safe fallback, where it means the
 * criteria are *unknown*), and `haptic: false` alongside `denial_risk: 'high'`
 * (an unverified answer, which must not buzz a device).
 */
export function nudgePayload(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    type: 'PAYER_RULE_ALERT',
    nudge_id: '0b7f0000-0000-4000-8000-000000000001',
    procedure: 'knee MRI',
    cpt_code: '73721',
    message: 'Prior authorization required for knee MRI. Still undocumented: six weeks of therapy.',
    missing_criteria: ['six weeks of conservative therapy'],
    denial_risk: 'high',
    haptic: true,
    ...overrides,
  };
}

/**
 * The fallback-shaped payload: high risk, no criteria, and deliberately no
 * escalation.
 *
 * `query.fallback_answer()` produces this when Qdrant is unreachable, Bedrock
 * errors, or retrieval matched nothing — the requirement genuinely is unverified,
 * so the risk is honestly `high`, and the nudge still fires. What is suppressed
 * is the buzz: a haptic alert earns its interruption by being rare, and an
 * outage that buzzes once per procedure in every concurrent encounter teaches a
 * physician to tune out the alert that means "this order will be denied".
 *
 * Named here because it is the fixture for the one test that pins that decision
 * down, and a reader of that test should not have to reconstruct why a `high`
 * payload arrives with `haptic: false`.
 */
export function fallbackNudgePayload(
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  return nudgePayload({
    message: 'Prior authorization could not be verified for knee MRI. Confirm manually.',
    missing_criteria: [],
    denial_risk: 'high',
    haptic: false,
    ...overrides,
  });
}
