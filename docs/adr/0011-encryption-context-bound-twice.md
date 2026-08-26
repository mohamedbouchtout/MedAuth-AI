# ADR-0011: Encryption context is bound twice — KMS and GCM AAD

**Status:** Accepted · **Task:** TASK-003

## Context

`crypto-utils` performs field-level AES-256-GCM encryption with a KMS-wrapped
data encryption key per record. Each call takes a `context: dict[str, str]`
identifying what is being encrypted — which record, which field.

The natural implementation passes that context to KMS as the encryption context
on the DEK wrap and unwrap. KMS then refuses to unwrap the DEK under a different
context, which looks like sufficient binding.

It is not. AES-GCM has no knowledge that its ciphertext was scoped to a record.
If the KMS-side check is ever bypassed — a code path that caches an unwrapped
DEK, a future refactor, a compromised call site — ciphertext from one record's
field can be moved onto another record and will decrypt cleanly.

## Decision

The same `context` is bound in **two** independent places:

1. as the **KMS encryption context** on the DEK wrap/unwrap call, and
2. as **AES-GCM's AAD** on the local encrypt/decrypt operation.

GCM's own authentication tag then rejects a mismatched context, independently of
whether KMS was consulted correctly. `decrypt_field()` raising on a context
mismatch is the intended behaviour, not an edge case to work around.

The AAD is a canonical serialisation of the context, so two dicts with the same
pairs in different insertion order produce the same AAD.

## Consequences

- A ciphertext swapped between records fails to decrypt at the cipher layer.
- The context is not optional and cannot be defaulted. Every call site states
  what the field belongs to, which is a small burden that keeps the guarantee.
- **Nothing here ever logs plaintext.** Not the field value, not the unwrapped
  DEK, not in an exception message or a stack trace. A failure names the context
  *keys* being processed and nothing else. This holds inside the crypto
  primitives, not merely at call sites that happen to touch PHI.
- All KMS mocking in tests uses moto's `@mock_aws`, never hand-rolled
  `unittest.mock` on boto3 calls — a hand-rolled mock cannot enforce the
  encryption-context check that half this decision depends on.

## References

- `packages/crypto-utils/src/crypto_utils/fields.py`, `context.py`, `kms.py`
- `CLAUDE.md` -> packages/crypto-utils — Design Decisions
