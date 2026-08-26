# ADR-0027: Only stabilized transcript segments are published

**Status:** Accepted · **Task:** TASK-020

## Context

AWS Transcribe emits a **partial** result for an utterance repeatedly as it
revises it — the same `result_id`, growing and changing, several times a second
— and then one **final** result with `is_partial` false.

Forwarding the partials would multiply bus traffic by an order of magnitude.
Worse, it would make the keyword scan fire the same procedure keyword over and
over as one sentence is re-transcribed, turning a single order into a stream of
duplicate nudges.

## Decision

`audio-ingestion` publishes only stabilized (non-partial) segments to
`transcription:{session_id}`.

The payload still carries an `is_partial` field, so a later task can widen this
to partials **without changing the message shape or the consumers' parsing**.

## Consequences

- Nudge latency is bounded below by utterance finalisation rather than by the
  first partial that contains the keyword. That is the right trade: a nudge for
  a sentence the speaker was still revising is a nudge about something that may
  not have been said.
- This is the first of two independent duplicate-suppression mechanisms. It
  removes duplicates *within* one utterance; ADR-0029 removes them *across*
  utterances in one encounter. Neither subsumes the other.
- The payload is PHI. It goes to Redis and nowhere else; the publisher logs that
  a segment was published and never what was in it.

## References

- `services/audio-ingestion/src/publisher.py`
