"""Shared fixtures for crypto-utils tests.

Every test that touches KMS runs against moto (see CLAUDE.md constraint 3) — no
hand-rolled mocking of boto3, and no real AWS call from CI.
"""

from __future__ import annotations

from collections.abc import Iterator

import boto3
import pytest
from moto import mock_aws

import crypto_utils
from crypto_utils import kms

_REGION = "us-east-1"


@pytest.fixture(autouse=True)
def _aws_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point boto3 at obviously fake credentials so no test can reach real AWS."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_REGION", _REGION)
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.delenv("KMS_KEK_ARN", raising=False)


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    """Clear the cached client and overrides so tests cannot leak into each other."""
    kms.configure(kek_arn=None, region_name=None)
    yield
    kms.configure(kek_arn=None, region_name=None)


@pytest.fixture
def kek_arn() -> Iterator[str]:
    """A moto-backed KMS key, configured as the package's KEK for the test's duration.

    The client is built inside the mock: ``configure()`` drops any cached client, so
    a client created before the mock started cannot survive into the test.
    """
    with mock_aws():
        client = boto3.client("kms", region_name=_REGION)
        arn = client.create_key(Description="MedAuth crypto-utils test KEK")["KeyMetadata"]["Arn"]
        crypto_utils.configure(kek_arn=arn, region_name=_REGION)
        yield str(arn)


@pytest.fixture
def context() -> dict[str, str]:
    """A representative encryption context: identifiers only, no field content."""
    return {
        "table": "clinical_notes",
        "record_id": "8f2b1c40-7d1e-4f9a-9c3b-2a1d5e6f7a80",
        "field": "body",
    }


@pytest.fixture
def other_context() -> dict[str, str]:
    """The same field on a different record — must never decrypt the first one's data."""
    return {
        "table": "clinical_notes",
        "record_id": "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d",
        "field": "body",
    }
