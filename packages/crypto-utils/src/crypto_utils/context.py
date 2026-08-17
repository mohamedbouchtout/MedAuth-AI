"""Encryption-context validation and canonical serialization.

The context is what ties a ciphertext to the record it belongs to. It is bound in
two places — as the KMS encryption context on the DEK wrap/unwrap, and as AES-GCM
additional authenticated data on the field itself. Both bindings are derived from
this one module, so the two can never drift apart.

The context is authenticated, not confidential: KMS stores it in the clear and it
is recoverable from CloudTrail. Put record identifiers in it, never a plaintext
field value.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

from .errors import InvalidEncryptionContextError


def validate_context(context: Mapping[str, str]) -> dict[str, str]:
    """Return `context` as a plain dict, rejecting anything KMS or GCM cannot bind.

    An empty context is refused rather than accepted as "no binding": it would
    encrypt successfully and leave the ciphertext scoped to nothing, which is the
    exact failure the two-place binding exists to prevent.

    Raises:
        InvalidEncryptionContextError: The context is not a mapping, is empty, or
            has a key or value that is not a non-empty string.
    """
    if not isinstance(context, Mapping):
        raise InvalidEncryptionContextError(
            f"encryption context must be a mapping of str to str, got {type(context).__name__}"
        )
    if not context:
        raise InvalidEncryptionContextError(
            "encryption context must not be empty — it is what scopes a ciphertext to its record"
        )

    validated: dict[str, str] = {}
    for key, value in context.items():
        # Keys are developer-chosen names such as "patient_id" and are safe to name in
        # an error. Values may be record identifiers, so they are never echoed.
        if not isinstance(key, str) or not key:
            raise InvalidEncryptionContextError("encryption context keys must be non-empty strings")
        if not isinstance(value, str):
            raise InvalidEncryptionContextError(
                f"encryption context value for {key!r} must be a str, got {type(value).__name__}"
            )
        validated[key] = value
    return validated


def context_keys(context: Mapping[str, str]) -> list[str]:
    """Return the context's keys, sorted — the only part of a context safe to log.

    Error messages and log lines identify a failing operation by its context keys.
    Values are withheld because they carry record identifiers.
    """
    return sorted(str(key) for key in context)


def canonical_aad(context: Mapping[str, str]) -> bytes:
    """Serialize a validated context to the exact bytes used as AES-GCM AAD.

    GCM compares AAD byte for byte, so the encoding has to be deterministic and
    unambiguous. JSON with sorted keys gives both: ordering cannot vary between
    encrypt and decrypt, and JSON's quoting means no pair of distinct contexts can
    collide on the same bytes.
    """
    validated = validate_context(context)
    serialized = json.dumps(validated, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return serialized.encode("utf-8")
