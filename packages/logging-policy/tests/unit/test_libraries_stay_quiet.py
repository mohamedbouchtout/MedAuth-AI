"""What the floors actually buy, asserted against the real libraries.

Every test here has two halves: the library leaks with the policy uninstalled,
and does not leak with it installed. The control half is the point. A test that
only asserted the quiet half would pass if the library had stopped logging for
some unrelated reason, if the request were never made, or if the needle simply
never reached the URL — which is how a logging test comes to assert nothing at
all. The pinning test this package deletes from ``fhir-integration`` existed for
the same reason, from the other direction.

The floors are not uniform, so neither are these tests: ``httpx`` leaks at INFO
and is floored to WARNING, while ``urllib3`` and ``botocore`` leak at DEBUG and
keep their INFO. Each test drives the level its library actually writes at.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer

import boto3
import httpx
import pytest
import urllib3
from moto import mock_aws

from logging_policy import LIBRARY_LOG_FLOORS, install_logging_policy

#: A string no library would produce on its own, standing in for the patient
#: data that reaches these call sites in the services: a name in a
#: ``Patient?name=`` search, an id in a ``Patient/{id}`` path, an encounter's
#: transcript in a Bedrock request body.
NEEDLE = "Sanchez-NEEDLE"


@pytest.fixture(autouse=True)
def unconfigured_libraries() -> Iterator[None]:
    """Start each test from "nobody has configured these", and restore after.

    That is the state this package found the repository in, and it is what makes
    the control half of each test a real leak rather than a contrived one.
    """
    saved = {name: logging.getLogger(name).level for name in LIBRARY_LOG_FLOORS}
    for name in LIBRARY_LOG_FLOORS:
        logging.getLogger(name).setLevel(logging.NOTSET)
    yield
    for name, level in saved.items():
        logging.getLogger(name).setLevel(level)


def leaking_records(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Every captured message carrying the needle, from any logger."""
    return [record.getMessage() for record in caplog.records if NEEDLE in record.getMessage()]


@contextmanager
def loopback_server() -> Iterator[int]:
    """A minimal HTTP server on an ephemeral port, for the real-socket cases.

    ``urllib3`` writes its request line after reading the response status, so
    unlike ``httpx`` there is no transport seam to substitute — the log line only
    happens if a real connection does.
    """

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args: object) -> None:
            """Silence the server's own stderr access log."""

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        thread.join(timeout=5)


def fetch_with_httpx(path: str) -> None:
    """Make one httpx request over a mock transport.

    A mock transport is enough here because httpx logs the request in its own
    client, before any transport is involved — which is also why the leak was
    reachable from every test in the repository, not only from a networked one.
    """
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    with httpx.Client(transport=transport, base_url="https://ehr.example.com/fhir") as client:
        client.get(path)


class TestHttpx:
    """The defect TASK-046 is about: the full URL, at INFO, by default."""

    def test_a_search_query_string_leaks_without_the_policy(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO):
            fetch_with_httpx(f"/Patient?name={NEEDLE}")

        assert leaking_records(caplog), "expected httpx to log the URL it was given"

    def test_a_search_query_string_is_quiet_with_it(self, caplog: pytest.LogCaptureFixture) -> None:
        install_logging_policy()

        with caplog.at_level(logging.DEBUG):
            fetch_with_httpx(f"/Patient?name={NEEDLE}")

        assert leaking_records(caplog) == []

    def test_a_patient_id_in_the_path_is_quiet_too(self, caplog: pytest.LogCaptureFixture) -> None:
        """The case a query-string sanitiser would have missed.

        ``get_patient()`` reads ``Patient/{patient_id}``, so the identifier is in
        the path on the most ordinary read the adapter makes.
        """
        install_logging_policy()

        with caplog.at_level(logging.DEBUG):
            fetch_with_httpx(f"/Patient/{NEEDLE}")

        assert leaking_records(caplog) == []

    def test_it_still_reports_a_failure(self) -> None:
        """WARNING and above survives — the floor is not a gag.

        Raising a library to WARNING is only acceptable if what remains is the
        part an operator needs. This asserts the level rather than a message,
        because the messages are httpx's to change.
        """
        install_logging_policy()

        assert logging.getLogger("httpx").isEnabledFor(logging.WARNING)
        assert logging.getLogger("httpx").isEnabledFor(logging.ERROR)


class TestUrllib3:
    """botocore's transport, and it writes the request line at DEBUG."""

    def test_the_request_line_leaks_without_the_policy(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with loopback_server() as port, caplog.at_level(logging.DEBUG):
            urllib3.PoolManager().request("GET", f"http://127.0.0.1:{port}/Patient?name={NEEDLE}")

        assert leaking_records(caplog), "expected urllib3 to log the request line"

    def test_it_is_quiet_with_the_policy(self, caplog: pytest.LogCaptureFixture) -> None:
        install_logging_policy()

        with loopback_server() as port, caplog.at_level(logging.DEBUG):
            urllib3.PoolManager().request("GET", f"http://127.0.0.1:{port}/Patient?name={NEEDLE}")

        assert leaking_records(caplog) == []


class TestBotocore:
    """Request *and* response bodies, both at DEBUG.

    KMS stands in for Bedrock and Comprehend Medical here: the leak is in
    ``botocore.endpoint`` and ``botocore.parsers``, which are shared by every
    service client, so any call proves the same thing — and KMS is one moto
    implements, per CLAUDE.md's rule that AWS mocking goes through moto.
    """

    @staticmethod
    def encrypt_something() -> None:
        """One real KMS round trip, with the needle in the encryption context.

        The context rather than the plaintext, and the distinction is worth
        knowing rather than incidental: KMS takes ``Plaintext`` as a blob, so
        botocore renders it base64-encoded in the logged body, while the
        encryption context is JSON and travels verbatim. ``crypto_utils`` binds
        record identifiers into exactly that context (CLAUDE.md, "Encryption
        context is bound in two places"), so the verbatim half is the half this
        repository actually fills with identifiers.
        """
        with mock_aws():
            kms = boto3.client("kms", region_name="us-east-1")
            key_id = kms.create_key(Description="policy test")["KeyMetadata"]["KeyId"]
            kms.encrypt(
                KeyId=key_id,
                Plaintext=b"a clinical field",
                EncryptionContext={"patient_id": NEEDLE},
            )

    def test_the_request_body_leaks_without_the_policy(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.DEBUG):
            self.encrypt_something()

        assert leaking_records(caplog), "expected botocore to log the request body"

    def test_it_is_quiet_with_the_policy(self, caplog: pytest.LogCaptureFixture) -> None:
        install_logging_policy()

        with caplog.at_level(logging.DEBUG):
            self.encrypt_something()

        assert leaking_records(caplog) == []

    def test_credential_resolution_still_reports(self) -> None:
        """The floor is INFO rather than WARNING for this reason.

        ``botocore.credentials`` says at INFO where credentials came from, which
        is operationally useful and carries nothing about a patient. Flooring the
        family to WARNING would have thrown that away to fix a DEBUG leak.
        """
        install_logging_policy()

        assert logging.getLogger("botocore.credentials").isEnabledFor(logging.INFO)


class TestHttpcore:
    """Floored, but on the record as not having leaked.

    Its trace lines render a request as the method and nothing else, and it logs
    no request headers, so this asserts what was actually verified rather than
    implying a vulnerability that was not there. A future version that starts
    rendering the URL is what the floor is for.
    """

    def test_it_does_not_carry_the_url_even_unfloored(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.DEBUG), loopback_server() as port:
            with httpx.Client() as client:
                client.get(f"http://127.0.0.1:{port}/Patient?name={NEEDLE}")

        traced = [
            record.getMessage() for record in caplog.records if record.name.startswith("httpcore")
        ]
        assert traced, "expected httpcore to trace the exchange"
        assert all(NEEDLE not in message for message in traced)
