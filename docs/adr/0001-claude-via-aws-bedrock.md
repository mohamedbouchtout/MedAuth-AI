# ADR-0001: Claude is called via AWS Bedrock, never the direct Anthropic API

**Status:** Accepted · **Task:** TASK-001 (established), TASK-012 (first call site)

## Context

MedAuth AI sends clinical text to a large language model: SOAP note generation
reads a transcript of a physician-patient encounter, and the policy query path
reasons over retrieved payer text on behalf of a named patient encounter. Any
path that could carry PHI to a third party needs that third party to be covered
by a Business Associate Agreement.

Anthropic's direct API and AWS Bedrock both serve the same Claude models. They
are not equivalent for this system: AWS is a HIPAA-eligible provider with a
signed BAA covering the services in `us-east-1`, and Bedrock is one of them.

## Decision

Every Claude call goes through AWS Bedrock in `us-east-1`, reached through
`langchain_aws.ChatBedrock`. No service imports the `anthropic` package.

Model selection is by role, never by literal: extraction uses Haiku via
`BEDROCK_MODEL_ID_FAST`, reasoning uses Sonnet via `BEDROCK_MODEL_ID_REASONING`.
A model id is never written as a string in application code.

## Consequences

- Switching models is an environment change, not a code change.
- Bedrock is the one AWS service called during local development; there is no
  local mock, so `pytest` mocks it with **moto** and never reaches the network.
- Bedrock's regional model availability constrains which models can be used at
  all, and new Anthropic models arrive here later than on the direct API. That
  latency is the price of the BAA and is accepted.
- The Haiku/Sonnet split is per call site rather than per service, and each site
  is named in `CLAUDE.md`'s Bedrock Model Assignment table. Extraction on Sonnet
  would cost roughly fifteen times as much for no gain in quality.

## References

- `services/track-b-rag/src/track_b_rag/bedrock.py`
- `CLAUDE.md` -> Key Architectural Constraints; Bedrock Model Assignment
