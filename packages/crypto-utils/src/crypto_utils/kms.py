"""KMS key encryption key handling.

The KEK never leaves KMS. Each record gets its own data encryption key: KMS mints
one with ``GenerateDataKey`` and hands back both a plaintext copy and a copy
wrapped under the KEK. The plaintext DEK encrypts the field and is dropped; the
wrapped copy is stored alongside the ciphertext and can only be unwrapped by KMS,
under the same encryption context.

Nothing in this module logs key material. Botocore error messages can echo the
encryption context back, so KMS failures are re-raised carrying the AWS error code
only — never the full message.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from typing import Any, Final, NamedTuple

# boto3 and botocore ship no py.typed marker, so mypy has no stubs for them. The
# suppression lives in the root pyproject's mypy overrides rather than here: this
# package was the only AWS caller until track-b-rag started calling Bedrock in
# TASK-012, and two import sites carrying the same local ignore is how a
# workspace-wide decision ends up written down in two places that can disagree.
import boto3
from botocore.exceptions import BotoCoreError, ClientError

from .context import context_keys, validate_context
from .errors import CryptoConfigurationError, DecryptionError, EncryptionError

logger: Final = logging.getLogger(__name__)

# AES-256 — the only key spec this package uses. AES-128 would silently weaken
# every field, so it is not offered as a parameter.
_DEK_KEY_SPEC: Final[str] = "AES_256"
_EXPECTED_DEK_BYTES: Final[int] = 32

_client: Any | None = None
_kek_arn_override: str | None = None
_region_override: str | None = None


class DataKey(NamedTuple):
    """One freshly minted data encryption key.

    Attributes:
        plaintext: The raw 32-byte key. Use it and drop it — never store or log it.
        wrapped: The same key encrypted under the KEK, safe to store beside the
            ciphertext.
        key_id: ARN of the KEK that wrapped it, recorded so decrypt can name the
            key and so a rotation can tell which fields still use the old KEK.
    """

    plaintext: bytes
    wrapped: bytes
    key_id: str


def configure(*, kek_arn: str | None = None, region_name: str | None = None) -> None:
    """Override the KEK ARN and region that would otherwise come from the environment.

    Passing ``None`` for either clears that override. The cached client is dropped
    so a region change takes effect on the next call.
    """
    global _kek_arn_override, _region_override
    _kek_arn_override = kek_arn
    _region_override = region_name
    reset_client()


def resolve_kek_arn() -> str:
    """Return the configured KEK ARN, from the override or ``KMS_KEK_ARN``."""
    arn = _kek_arn_override if _kek_arn_override is not None else os.environ.get("KMS_KEK_ARN")
    if not arn:
        raise CryptoConfigurationError(
            "No KMS key encryption key configured for crypto-utils: set KMS_KEK_ARN "
            "or call configure(kek_arn=...)."
        )
    return arn


def _resolve_region() -> str | None:
    """Return the region for the KMS client, or None to use boto3's own resolution."""
    if _region_override is not None:
        return _region_override
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")


def set_client(client: Any | None) -> None:
    """Install a KMS client for every subsequent call, bypassing lazy creation.

    Passing ``None`` restores the lazily created client.
    """
    global _client
    _client = client


def reset_client() -> None:
    """Drop the cached client so the next call builds a fresh one.

    Tests need this: a client built outside a moto mock keeps talking to the real
    endpoint after the mock starts.
    """
    global _client
    _client = None


def get_client() -> Any:
    """Return the KMS client, creating it on first use."""
    global _client
    if _client is None:
        _client = boto3.client("kms", region_name=_resolve_region())
    return _client


def _require_bytes(value: object, field: str) -> bytes:
    """Narrow an untyped botocore response field to bytes."""
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray | memoryview):
        return bytes(value)
    raise EncryptionError(f"KMS returned no usable {field} in its response")


def _require_str(value: object, field: str) -> str:
    """Narrow an untyped botocore response field to str."""
    if isinstance(value, str) and value:
        return value
    raise EncryptionError(f"KMS returned no usable {field} in its response")


def _aws_error_code(exc: ClientError) -> str:
    """Return the AWS error code alone.

    The full botocore message can quote the encryption context back, and context
    values carry record identifiers — so only the code crosses into our exceptions.
    """
    response = getattr(exc, "response", None)
    if isinstance(response, Mapping):
        error = response.get("Error")
        if isinstance(error, Mapping):
            code = error.get("Code")
            if isinstance(code, str) and code:
                return code
    return "Unknown"


def generate_data_key(context: Mapping[str, str]) -> DataKey:
    """Mint a per-record DEK, bound to `context` as the KMS encryption context.

    Raises:
        CryptoConfigurationError: No KEK is configured.
        InvalidEncryptionContextError: The context is empty or not all strings.
        EncryptionError: KMS refused or could not be reached.
    """
    validated = validate_context(context)
    kek_arn = resolve_kek_arn()
    try:
        response = get_client().generate_data_key(
            KeyId=kek_arn,
            KeySpec=_DEK_KEY_SPEC,
            EncryptionContext=validated,
        )
    except ClientError as exc:
        raise EncryptionError(
            f"KMS refused to generate a data key ({_aws_error_code(exc)}) for context "
            f"keys {context_keys(validated)}"
        ) from None
    except BotoCoreError as exc:
        raise EncryptionError(f"Could not reach KMS to generate a data key: {exc}") from None

    plaintext = _require_bytes(response.get("Plaintext"), "Plaintext")
    if len(plaintext) != _EXPECTED_DEK_BYTES:
        raise EncryptionError(
            f"KMS returned a {len(plaintext)}-byte data key, expected {_EXPECTED_DEK_BYTES}"
        )
    return DataKey(
        plaintext=plaintext,
        wrapped=_require_bytes(response.get("CiphertextBlob"), "CiphertextBlob"),
        key_id=_require_str(response.get("KeyId"), "KeyId"),
    )


def unwrap_data_key(wrapped: bytes, context: Mapping[str, str], key_id: str | None = None) -> bytes:
    """Recover a DEK from its wrapped form, under the same encryption context.

    KMS rejects the call outright if `context` differs from the one the key was
    wrapped with — the first of the two context bindings.

    Args:
        wrapped: The DEK as stored, still encrypted under the KEK.
        context: Must match the context used at wrap time.
        key_id: KEK the field recorded. Passing it stops a substituted blob from
            being unwrapped under some other key the caller happens to hold.

    Raises:
        InvalidEncryptionContextError: The context is empty or not all strings.
        DecryptionError: KMS refused the unwrap, the context did not match, or KMS
            could not be reached.
    """
    validated = validate_context(context)
    request: dict[str, Any] = {
        "CiphertextBlob": wrapped,
        "EncryptionContext": validated,
    }
    if key_id:
        request["KeyId"] = key_id
    try:
        response = get_client().decrypt(**request)
    except ClientError as exc:
        raise DecryptionError(
            f"KMS refused to unwrap the data key ({_aws_error_code(exc)}) for context "
            f"keys {context_keys(validated)} — the encryption context may not match"
        ) from None
    except BotoCoreError as exc:
        raise DecryptionError(f"Could not reach KMS to unwrap the data key: {exc}") from None

    plaintext = response.get("Plaintext")
    if not isinstance(plaintext, bytes | bytearray | memoryview):
        raise DecryptionError("KMS returned no usable Plaintext in its response")
    return bytes(plaintext)
