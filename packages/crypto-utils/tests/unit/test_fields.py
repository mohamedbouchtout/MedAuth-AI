"""Unit tests for encrypt_field / decrypt_field, against moto-backed KMS."""

from __future__ import annotations

import base64
import os

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from crypto_utils import (
    DecryptionError,
    EncryptedField,
    EncryptionError,
    InvalidEncryptionContextError,
    canonical_aad,
    decrypt_field,
    encrypt_field,
    kms,
)

_NOTE = "Patient reports left knee pain, worse on stairs. MRI ordered."


def test_round_trip(kek_arn: str, context: dict[str, str]) -> None:
    assert decrypt_field(encrypt_field(_NOTE, context), context) == _NOTE


@pytest.mark.parametrize(
    "plaintext",
    ["", "x", _NOTE, "Ángel Muñoz — ICD-10 M25.562", "line one\nline two\ttabbed"],
)
def test_round_trip_preserves_the_exact_value(
    kek_arn: str, context: dict[str, str], plaintext: str
) -> None:
    assert decrypt_field(encrypt_field(plaintext, context), context) == plaintext


def test_envelope_never_contains_the_plaintext(kek_arn: str, context: dict[str, str]) -> None:
    encrypted = encrypt_field(_NOTE, context)
    serialized = encrypted.model_dump_json()
    assert _NOTE not in serialized
    assert "knee" not in serialized
    # The whole envelope is safe to log, so its repr must be clean too.
    assert "knee" not in repr(encrypted)


def test_encrypting_the_same_value_twice_gives_different_ciphertext(
    kek_arn: str, context: dict[str, str]
) -> None:
    """Fresh DEK and fresh nonce per call — equal plaintexts must not look equal."""
    first = encrypt_field(_NOTE, context)
    second = encrypt_field(_NOTE, context)
    assert first.ciphertext != second.ciphertext
    assert first.nonce != second.nonce
    assert first.wrapped_dek != second.wrapped_dek


def test_nonce_is_twelve_bytes(kek_arn: str, context: dict[str, str]) -> None:
    encrypted = encrypt_field(_NOTE, context)
    assert len(base64.b64decode(encrypted.nonce, validate=True)) == 12


def test_envelope_records_the_kek_and_the_format(kek_arn: str, context: dict[str, str]) -> None:
    encrypted = encrypt_field(_NOTE, context)
    assert encrypted.key_id == kek_arn
    assert encrypted.algorithm == "AES-256-GCM"
    assert encrypted.version == 1


def test_envelope_is_immutable(kek_arn: str, context: dict[str, str]) -> None:
    encrypted = encrypt_field(_NOTE, context)
    with pytest.raises(Exception, match="frozen"):
        encrypted.ciphertext = "tampered"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# The two context bindings. Each is tested on its own, because the whole point of
# binding twice is that either one alone would stop the attack.
# ---------------------------------------------------------------------------


def test_a_different_context_cannot_decrypt(
    kek_arn: str, context: dict[str, str], other_context: dict[str, str]
) -> None:
    encrypted = encrypt_field(_NOTE, context)
    with pytest.raises(DecryptionError):
        decrypt_field(encrypted, other_context)


def test_gcm_aad_rejects_a_mismatch_that_kms_would_have_allowed(
    kek_arn: str, context: dict[str, str], other_context: dict[str, str]
) -> None:
    """The second binding, isolated.

    The envelope below has its DEK wrapped under ``other_context``, so the KMS
    unwrap succeeds — exactly the situation the KMS-side check cannot catch. Only
    the GCM AAD, which was computed from ``context``, stops the decrypt.
    """
    data_key = kms.generate_data_key(other_context)
    nonce = os.urandom(12)
    blob = AESGCM(data_key.plaintext).encrypt(nonce, _NOTE.encode(), canonical_aad(context))
    mismatched = EncryptedField(
        ciphertext=base64.b64encode(blob).decode("ascii"),
        nonce=base64.b64encode(nonce).decode("ascii"),
        wrapped_dek=base64.b64encode(data_key.wrapped).decode("ascii"),
        key_id=data_key.key_id,
    )

    # KMS is satisfied: the context it was asked about is the one the DEK was wrapped with.
    assert kms.unwrap_data_key(data_key.wrapped, other_context, data_key.key_id)

    with pytest.raises(DecryptionError, match="Authentication failed"):
        decrypt_field(mismatched, other_context)


def test_ciphertext_cannot_be_moved_onto_another_record(
    kek_arn: str, context: dict[str, str], other_context: dict[str, str]
) -> None:
    """The attack the two-place binding exists to prevent."""
    mine = encrypt_field(_NOTE, context)
    theirs = encrypt_field("Unrelated note", other_context)
    swapped = theirs.model_copy(update={"ciphertext": mine.ciphertext, "nonce": mine.nonce})
    with pytest.raises(DecryptionError):
        decrypt_field(swapped, other_context)


def test_decrypt_failure_names_context_keys_but_no_values(
    kek_arn: str, context: dict[str, str], other_context: dict[str, str]
) -> None:
    encrypted = encrypt_field(_NOTE, context)
    with pytest.raises(DecryptionError) as caught:
        decrypt_field(encrypted, other_context)
    message = str(caught.value)
    assert "record_id" in message
    assert other_context["record_id"] not in message
    assert _NOTE not in message


# ---------------------------------------------------------------------------
# Malformed and tampered envelopes
# ---------------------------------------------------------------------------


def test_a_tampered_ciphertext_is_rejected(kek_arn: str, context: dict[str, str]) -> None:
    encrypted = encrypt_field(_NOTE, context)
    raw = bytearray(base64.b64decode(encrypted.ciphertext, validate=True))
    raw[0] ^= 0x01
    tampered = encrypted.model_copy(
        update={"ciphertext": base64.b64encode(bytes(raw)).decode("ascii")}
    )
    with pytest.raises(DecryptionError, match="Authentication failed"):
        decrypt_field(tampered, context)


def test_a_tampered_nonce_is_rejected(kek_arn: str, context: dict[str, str]) -> None:
    encrypted = encrypt_field(_NOTE, context)
    raw = bytearray(base64.b64decode(encrypted.nonce, validate=True))
    raw[0] ^= 0x01
    tampered = encrypted.model_copy(update={"nonce": base64.b64encode(bytes(raw)).decode("ascii")})
    with pytest.raises(DecryptionError, match="Authentication failed"):
        decrypt_field(tampered, context)


@pytest.mark.parametrize("field", ["ciphertext", "nonce", "wrapped_dek"])
def test_malformed_base64_names_the_field(
    kek_arn: str, context: dict[str, str], field: str
) -> None:
    encrypted = encrypt_field(_NOTE, context)
    broken = encrypted.model_copy(update={field: "not base64!!"})
    with pytest.raises(DecryptionError, match=f"EncryptedField.{field} is not valid base64"):
        decrypt_field(broken, context)


def test_an_unknown_format_version_is_rejected(kek_arn: str, context: dict[str, str]) -> None:
    encrypted = encrypt_field(_NOTE, context)
    future = encrypted.model_copy(update={"version": 2})
    with pytest.raises(DecryptionError, match="Unsupported envelope"):
        decrypt_field(future, context)


def test_an_unknown_algorithm_is_rejected(kek_arn: str, context: dict[str, str]) -> None:
    encrypted = encrypt_field(_NOTE, context)
    other = encrypted.model_copy(update={"algorithm": "AES-128-GCM"})
    with pytest.raises(DecryptionError, match="Unsupported envelope"):
        decrypt_field(other, context)


def test_non_utf8_content_is_reported_without_the_bytes(
    kek_arn: str, context: dict[str, str]
) -> None:
    """A field written by something other than this package may not decode.

    Built the same way a real envelope is, but over bytes that are not UTF-8, so the
    cipher succeeds and only the decode step fails.
    """
    data_key = kms.generate_data_key(context)
    nonce = os.urandom(12)
    blob = AESGCM(data_key.plaintext).encrypt(nonce, b"\xff\xfe\x00binary", canonical_aad(context))
    envelope = EncryptedField(
        ciphertext=base64.b64encode(blob).decode("ascii"),
        nonce=base64.b64encode(nonce).decode("ascii"),
        wrapped_dek=base64.b64encode(data_key.wrapped).decode("ascii"),
        key_id=data_key.key_id,
    )
    with pytest.raises(DecryptionError, match="not valid UTF-8") as caught:
        decrypt_field(envelope, context)
    assert "binary" not in str(caught.value)


def test_an_envelope_rejects_unknown_fields() -> None:
    with pytest.raises(ValueError, match="extra_forbidden|Extra inputs"):
        EncryptedField(
            ciphertext="AA==",
            nonce="AA==",
            wrapped_dek="AA==",
            key_id="arn",
            plaintext_backup="oops",  # type: ignore[call-arg]
        )


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def test_encrypt_rejects_an_empty_context(kek_arn: str) -> None:
    with pytest.raises(InvalidEncryptionContextError, match="must not be empty"):
        encrypt_field(_NOTE, {})


def test_decrypt_rejects_an_empty_context(kek_arn: str, context: dict[str, str]) -> None:
    encrypted = encrypt_field(_NOTE, context)
    with pytest.raises(InvalidEncryptionContextError, match="must not be empty"):
        decrypt_field(encrypted, {})


def test_encrypt_rejects_a_non_string_plaintext(kek_arn: str, context: dict[str, str]) -> None:
    with pytest.raises(EncryptionError, match="expects a str plaintext"):
        encrypt_field(b"already bytes", context)  # type: ignore[arg-type]


def test_encrypt_validates_the_context_before_calling_kms(context: dict[str, str]) -> None:
    """No KEK is configured here, so reaching KMS at all would raise a different error."""
    with pytest.raises(InvalidEncryptionContextError):
        encrypt_field(_NOTE, {})
