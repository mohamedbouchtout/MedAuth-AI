"""Unit tests for encryption-context validation and canonical serialization."""

from __future__ import annotations

from typing import Any

import pytest

from crypto_utils import canonical_aad, validate_context
from crypto_utils.context import context_keys
from crypto_utils.errors import InvalidEncryptionContextError


def test_canonical_aad_ignores_key_insertion_order() -> None:
    forwards = {"table": "clinical_notes", "record_id": "42", "field": "body"}
    backwards = {"field": "body", "record_id": "42", "table": "clinical_notes"}
    assert canonical_aad(forwards) == canonical_aad(backwards)


def test_canonical_aad_differs_for_different_records() -> None:
    assert canonical_aad({"record_id": "42"}) != canonical_aad({"record_id": "43"})


def test_canonical_aad_cannot_collide_across_key_boundaries() -> None:
    """A naive "key=value" join would render both of these as ``a=b:c``."""
    assert canonical_aad({"a": "b:c"}) != canonical_aad({"a:b": "c"})


def test_canonical_aad_survives_non_ascii_values() -> None:
    aad = canonical_aad({"clinician": "Dr Ángel Muñoz"})
    assert "Ángel".encode() in aad


def test_validate_context_returns_a_plain_dict() -> None:
    validated = validate_context({"record_id": "42"})
    assert validated == {"record_id": "42"}
    assert type(validated) is dict


def test_empty_context_is_rejected() -> None:
    with pytest.raises(InvalidEncryptionContextError, match="must not be empty"):
        validate_context({})


def test_non_mapping_context_is_rejected() -> None:
    with pytest.raises(InvalidEncryptionContextError, match="must be a mapping"):
        validate_context("record_id=42")  # type: ignore[arg-type]


def test_empty_key_is_rejected() -> None:
    with pytest.raises(InvalidEncryptionContextError, match="non-empty strings"):
        validate_context({"": "42"})


def test_non_string_key_is_rejected() -> None:
    bad: dict[Any, Any] = {7: "42"}
    with pytest.raises(InvalidEncryptionContextError, match="non-empty strings"):
        validate_context(bad)


def test_non_string_value_is_rejected_without_echoing_it() -> None:
    bad: dict[str, Any] = {"record_id": 1234567890}
    with pytest.raises(InvalidEncryptionContextError) as caught:
        validate_context(bad)
    message = str(caught.value)
    assert "record_id" in message
    assert "must be a str" in message
    # The key names the problem; the value is withheld because a context value is a
    # record identifier.
    assert "1234567890" not in message


def test_context_keys_are_sorted_and_values_are_absent() -> None:
    keys = context_keys({"table": "clinical_notes", "record_id": "42", "field": "body"})
    assert keys == ["field", "record_id", "table"]
    assert "clinical_notes" not in keys
