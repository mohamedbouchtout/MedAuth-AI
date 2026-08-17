"""Field-level encryption for sensitive columns.

AES-256-GCM with a per-record data encryption key, wrapped by a key encryption key
that never leaves AWS KMS. This encrypts specific fields before they reach the
database — it does not replace encryption at rest (RDS and S3 handle that), and it
is not a general crypto toolkit.

Typical use::

    from crypto_utils import encrypt_field, decrypt_field

    context = {"table": "clinical_notes", "record_id": str(note_id), "field": "body"}
    stored = encrypt_field(note_body, context)
    ...
    note_body = decrypt_field(stored, context)

The same context must be supplied to decrypt. It is bound both as the KMS
encryption context and as AES-GCM additional authenticated data, so a field
encrypted for one record will not decrypt under another's. A mismatch raises
:class:`DecryptionError` — that is the intended behavior, not an edge case.

The context is authenticated but not confidential: KMS stores it in the clear.
Scope a field with identifiers, never with a plaintext value.
"""

from .context import canonical_aad, validate_context
from .errors import (
    CryptoConfigurationError,
    CryptoError,
    DecryptionError,
    EncryptionError,
    InvalidEncryptionContextError,
)
from .fields import EncryptedField, decrypt_field, encrypt_field
from .kms import configure, reset_client, resolve_kek_arn, set_client

__all__ = [
    "CryptoConfigurationError",
    "CryptoError",
    "DecryptionError",
    "EncryptedField",
    "EncryptionError",
    "InvalidEncryptionContextError",
    "canonical_aad",
    "configure",
    "decrypt_field",
    "encrypt_field",
    "reset_client",
    "resolve_kek_arn",
    "set_client",
    "validate_context",
]
