"""Field-level AES-256-GCM encryption.

One field, one DEK, one nonce. The encryption context is bound twice — once by KMS
on the DEK, once by GCM on the ciphertext — so a field encrypted for one record
cannot be decrypted under another record's context even if the KMS-side check is
somehow bypassed. See CLAUDE.md, "packages/crypto-utils — Design Decisions".

Nothing here logs, raises, or returns a plaintext value or unwrapped key material.
Failures are identified by their context keys instead.
"""

from __future__ import annotations

import base64
import os
from typing import Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import BaseModel, ConfigDict

from . import kms
from .context import canonical_aad, context_keys
from .errors import DecryptionError, EncryptionError

# GCM's standard nonce length. Any other length costs the security proof that comes
# with the 96-bit construction, so it is fixed rather than configurable.
_NONCE_BYTES: Final[int] = 12

_ALGORITHM: Final[str] = "AES-256-GCM"
_FORMAT_VERSION: Final[int] = 1


class EncryptedField(BaseModel):
    """One encrypted field value, ready to store.

    Every attribute is safe to persist and safe to log: the plaintext is not
    recoverable from them without both KMS and the original encryption context.

    ``version`` and ``algorithm`` are plain ``int``/``str`` rather than constrained
    literals so that an envelope written by a future format still parses and fails
    with a clear :class:`DecryptionError`, instead of a validation error at load.

    Attributes:
        version: Envelope format version.
        algorithm: Cipher used to produce ``ciphertext``.
        ciphertext: Base64 of the GCM output, authentication tag appended.
        nonce: Base64 of the 12-byte nonce. Unique per encryption, not secret.
        wrapped_dek: Base64 of the DEK as encrypted by KMS under the KEK.
        key_id: ARN of the KEK, so a rotation can find fields still on an old key.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int = _FORMAT_VERSION
    algorithm: str = _ALGORITHM
    ciphertext: str
    nonce: str
    wrapped_dek: str
    key_id: str


def _b64decode(value: str, field: str) -> bytes:
    """Decode one base64 envelope field, naming the field rather than its content."""
    try:
        return base64.b64decode(value, validate=True)
    except ValueError:
        # binascii.Error subclasses ValueError and quotes the offending input, so the
        # cause is dropped rather than chained.
        raise DecryptionError(f"EncryptedField.{field} is not valid base64") from None


def encrypt_field(plaintext: str, context: dict[str, str]) -> EncryptedField:
    """Encrypt one field value under a fresh per-record data key.

    `context` is bound twice: as the KMS encryption context on the data key, and as
    AES-GCM additional authenticated data on the ciphertext. Both must be supplied
    again, identical, to decrypt.

    Args:
        plaintext: The value to encrypt. Never logged, never echoed in an error.
        context: Identifiers scoping this field to its record, e.g.
            ``{"table": "clinical_notes", "record_id": str(note_id), "field": "body"}``.
            Authenticated but not confidential — KMS stores it in the clear, so it
            must not contain a plaintext field value.

    Returns:
        An :class:`EncryptedField` safe to persist.

    Raises:
        CryptoConfigurationError: No KEK is configured.
        InvalidEncryptionContextError: The context is empty or not all strings.
        EncryptionError: KMS refused, or the cipher failed.
    """
    if not isinstance(plaintext, str):
        raise EncryptionError(
            f"encrypt_field expects a str plaintext, got {type(plaintext).__name__}"
        )

    aad = canonical_aad(context)
    data_key = kms.generate_data_key(context)
    # Read the storable halves out before the try, so the whole DataKey — and with it
    # the only reference to the plaintext DEK — can be dropped in the finally.
    wrapped = data_key.wrapped
    key_id = data_key.key_id
    nonce = os.urandom(_NONCE_BYTES)

    try:
        blob = AESGCM(data_key.plaintext).encrypt(nonce, plaintext.encode("utf-8"), aad)
    except Exception:
        # The cause is dropped rather than chained: nothing a cipher failure can add
        # is worth the risk of a message that quotes what it was given.
        raise EncryptionError(
            f"Failed to encrypt field for context keys {context_keys(context)}"
        ) from None
    finally:
        # CPython cannot wipe an immutable bytes object; this drops the last reference
        # so the key becomes collectable. It is not a secure erase — the real control
        # is that the DEK is per-record, used once, and never persisted in the clear.
        del data_key

    return EncryptedField(
        ciphertext=base64.b64encode(blob).decode("ascii"),
        nonce=base64.b64encode(nonce).decode("ascii"),
        wrapped_dek=base64.b64encode(wrapped).decode("ascii"),
        key_id=key_id,
    )


def decrypt_field(encrypted: EncryptedField, context: dict[str, str]) -> str:
    """Recover a field's plaintext, requiring the same context it was encrypted with.

    A context mismatch raises. It is caught twice over: KMS refuses to unwrap the
    data key, and if that check were ever bypassed, GCM's authentication tag rejects
    the ciphertext on its own.

    Args:
        encrypted: The stored envelope.
        context: Must equal the context passed to :func:`encrypt_field`.

    Returns:
        The original plaintext.

    Raises:
        InvalidEncryptionContextError: The context is empty or not all strings.
        DecryptionError: The context did not match, the envelope was altered or is
            malformed, or KMS refused the unwrap.
    """
    aad = canonical_aad(context)

    if encrypted.version != _FORMAT_VERSION or encrypted.algorithm != _ALGORITHM:
        raise DecryptionError(
            f"Unsupported envelope: version {encrypted.version}, algorithm {encrypted.algorithm!r}"
        )

    nonce = _b64decode(encrypted.nonce, "nonce")
    blob = _b64decode(encrypted.ciphertext, "ciphertext")
    wrapped = _b64decode(encrypted.wrapped_dek, "wrapped_dek")

    dek = kms.unwrap_data_key(wrapped, context, encrypted.key_id)
    try:
        recovered = AESGCM(dek).decrypt(nonce, blob, aad)
    except InvalidTag:
        raise DecryptionError(
            f"Authentication failed for context keys {context_keys(context)} — the "
            "encryption context does not match, or the ciphertext was altered"
        ) from None
    except Exception:
        raise DecryptionError(
            f"Failed to decrypt field for context keys {context_keys(context)}"
        ) from None
    finally:
        del dek

    try:
        return recovered.decode("utf-8")
    except UnicodeDecodeError:
        raise DecryptionError(
            f"Decrypted field for context keys {context_keys(context)} is not valid UTF-8"
        ) from None
