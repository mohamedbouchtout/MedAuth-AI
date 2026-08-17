"""Unit tests for KEK resolution and data key wrap/unwrap, against moto."""

from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

from crypto_utils import kms
from crypto_utils.errors import (
    CryptoConfigurationError,
    DecryptionError,
    EncryptionError,
    InvalidEncryptionContextError,
)


def test_resolve_kek_arn_reads_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KMS_KEK_ARN", "arn:aws:kms:us-east-1:123456789012:key/from-env")
    assert kms.resolve_kek_arn().endswith("from-env")


def test_configure_overrides_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KMS_KEK_ARN", "arn:aws:kms:us-east-1:123456789012:key/from-env")
    kms.configure(kek_arn="arn:aws:kms:us-east-1:123456789012:key/from-override")
    assert kms.resolve_kek_arn().endswith("from-override")


def test_resolve_kek_arn_raises_when_nothing_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KMS_KEK_ARN", raising=False)
    with pytest.raises(CryptoConfigurationError, match="No KMS key encryption key configured"):
        kms.resolve_kek_arn()


def test_generate_data_key_returns_a_256_bit_key(kek_arn: str, context: dict[str, str]) -> None:
    data_key = kms.generate_data_key(context)
    assert len(data_key.plaintext) == 32
    assert data_key.key_id == kek_arn
    # The wrapped copy is what gets stored, and it must not be the key itself.
    assert data_key.wrapped != data_key.plaintext


def test_each_call_mints_a_distinct_key(kek_arn: str, context: dict[str, str]) -> None:
    assert kms.generate_data_key(context).plaintext != kms.generate_data_key(context).plaintext


def test_unwrap_round_trips_under_the_same_context(kek_arn: str, context: dict[str, str]) -> None:
    data_key = kms.generate_data_key(context)
    recovered = kms.unwrap_data_key(data_key.wrapped, context, data_key.key_id)
    assert recovered == data_key.plaintext


def test_unwrap_refuses_a_different_context(
    kek_arn: str, context: dict[str, str], other_context: dict[str, str]
) -> None:
    """The first of the two bindings: KMS itself rejects the mismatch."""
    data_key = kms.generate_data_key(context)
    with pytest.raises(DecryptionError, match="encryption context may not match"):
        kms.unwrap_data_key(data_key.wrapped, other_context, data_key.key_id)


def test_unwrap_failure_names_context_keys_but_no_values(
    kek_arn: str, context: dict[str, str], other_context: dict[str, str]
) -> None:
    data_key = kms.generate_data_key(context)
    with pytest.raises(DecryptionError) as caught:
        kms.unwrap_data_key(data_key.wrapped, other_context, data_key.key_id)
    message = str(caught.value)
    assert "record_id" in message
    assert other_context["record_id"] not in message
    assert "clinical_notes" not in message


def test_unwrap_refuses_a_corrupted_blob(kek_arn: str, context: dict[str, str]) -> None:
    data_key = kms.generate_data_key(context)
    corrupted = bytes(reversed(data_key.wrapped))
    with pytest.raises(DecryptionError):
        kms.unwrap_data_key(corrupted, context, data_key.key_id)


def test_unwrap_works_without_a_key_id(kek_arn: str, context: dict[str, str]) -> None:
    """key_id is an extra guard, not a requirement — a symmetric unwrap works without it."""
    data_key = kms.generate_data_key(context)
    assert kms.unwrap_data_key(data_key.wrapped, context) == data_key.plaintext


def test_generate_data_key_rejects_an_empty_context(kek_arn: str) -> None:
    with pytest.raises(InvalidEncryptionContextError):
        kms.generate_data_key({})


def test_generate_data_key_surfaces_a_kms_refusal(context: dict[str, str]) -> None:
    """An unknown KEK is a client error, and must arrive as EncryptionError."""
    with mock_aws():
        kms.configure(
            kek_arn="arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000000",
            region_name="us-east-1",
        )
        with pytest.raises(EncryptionError, match="KMS refused to generate a data key"):
            kms.generate_data_key(context)


def test_kms_refusal_carries_the_error_code_not_the_full_message(
    context: dict[str, str],
) -> None:
    with mock_aws():
        kms.configure(
            kek_arn="arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000000",
            region_name="us-east-1",
        )
        with pytest.raises(EncryptionError) as caught:
            kms.generate_data_key(context)
    message = str(caught.value)
    assert "NotFoundException" in message
    assert context["record_id"] not in message


def test_set_client_installs_an_explicit_client(context: dict[str, str]) -> None:
    """The injection hook lets a caller supply its own configured client."""
    with mock_aws():
        client = boto3.client("kms", region_name="us-west-2")
        arn = client.create_key()["KeyMetadata"]["Arn"]
        kms.configure(kek_arn=arn, region_name="us-west-2")
        kms.set_client(client)
        assert kms.get_client() is client
        assert len(kms.generate_data_key(context).plaintext) == 32
    kms.set_client(None)


def test_reset_client_drops_the_cached_client(kek_arn: str) -> None:
    first = kms.get_client()
    assert kms.get_client() is first
    kms.reset_client()
    assert kms.get_client() is not first
