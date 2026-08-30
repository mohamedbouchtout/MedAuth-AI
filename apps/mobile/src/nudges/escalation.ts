/**
 * Whether a nudge escalates to a haptic alert.
 *
 * This is one line, and it has a module of its own on purpose: it is the only
 * place in this app that decides whether the device buzzes, so it is the only
 * place a future change could reintroduce the behaviour CLAUDE.md's "The nudge
 * payload — one shape" forbids.
 *
 * **The rule is `haptic`, and nothing else — never `denialRisk === 'high'`.**
 * The two look interchangeable in the payload and are not. `should_escalate()`
 * in track-b-rag withholds `haptic` on a high-risk answer it could not verify:
 * `query.fallback_answer()` sets `denial_risk="high"` for an unreachable Qdrant,
 * a Bedrock error, or a retrieval that matched nothing, and every one of those
 * nudges says only "confirm manually". A client that re-derived the buzz from
 * the risk level would turn one infrastructure outage into a device buzzing once
 * per procedure in every concurrent encounter.
 *
 * The reasoning is about the signal rather than the annoyance: a haptic alert
 * earns its interruption by being rare and meaning something, and a physician
 * who learns that the buzz usually means "our vendor is down" has been taught to
 * ignore the one that means "this order will be denied". An outage must not be
 * able to spend the credibility that genuinely high-risk nudges depend on.
 *
 * The emitter has already made this judgement with information this app does not
 * have — whether the answer came from a policy or from the fallback. Reading the
 * field is deference to that, not laziness.
 */

import type { Nudge } from '@medauth/nudge-client';

/**
 * True when this nudge should buzz the device.
 *
 * Do not add a condition here. In particular, do not add `denialRisk === 'high'`
 * as a second trigger: the emitter withholds `haptic` on exactly the high-risk
 * answers that must not buzz, so an `||` here inverts the decision it is
 * deferring to.
 */
export function shouldBuzz(nudge: Nudge): boolean {
  return nudge.haptic;
}
