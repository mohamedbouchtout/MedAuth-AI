"""Exceptions raised by field encryption.

Every message in this module is written on the assumption that it will be logged.
None of them may contain a plaintext field value or unwrapped key material — see
CLAUDE.md, "packages/crypto-utils — Design Decisions".
"""

from __future__ import annotations


class CryptoError(RuntimeError):
    """Base class for every field-encryption failure."""


class CryptoConfigurationError(CryptoError):
    """Raised when no KMS key encryption key is configured."""


class InvalidEncryptionContextError(CryptoError, ValueError):
    """Raised when an encryption context is missing, empty, or not all strings."""


class EncryptionError(CryptoError):
    """Raised when a field could not be encrypted."""


class DecryptionError(CryptoError):
    """Raised when a field could not be decrypted.

    A mismatched encryption context lands here. That is the intended outcome, not
    an edge case — a field encrypted for one record must never decrypt under
    another record's context.
    """
