"""Unit tests for parsing and matching the configured origin list."""

from __future__ import annotations

import pytest

from cors_policy import CorsPolicyError, is_allowed_origin, parse_allowed_origins


class TestParseAllowedOrigins:
    def test_splits_on_commas_and_strips_whitespace(self) -> None:
        assert parse_allowed_origins("http://localhost:5173, https://app.example.com") == (
            "http://localhost:5173",
            "https://app.example.com",
        )

    def test_drops_empty_entries_so_a_trailing_comma_is_not_an_origin(self) -> None:
        assert parse_allowed_origins("http://localhost:5173,,  ,") == ("http://localhost:5173",)

    def test_empty_value_is_no_origins_rather_than_every_origin(self) -> None:
        assert parse_allowed_origins("") == ()
        assert parse_allowed_origins("   ") == ()

    def test_wildcard_is_rejected(self) -> None:
        """CLAUDE.md forbids `*` on a service answering with PHI, and every
        service installing this package answers with PHI. The refusal lives in
        the primitive so no call site has to remember it."""
        with pytest.raises(CorsPolicyError, match="may not contain"):
            parse_allowed_origins("*")

    def test_wildcard_is_rejected_even_alongside_real_origins(self) -> None:
        with pytest.raises(CorsPolicyError):
            parse_allowed_origins("https://app.example.com, *")


class TestIsAllowedOrigin:
    ALLOWED = ("https://app.example.com", "http://localhost:5173")

    def test_configured_origin_is_allowed(self) -> None:
        assert is_allowed_origin("https://app.example.com", self.ALLOWED)

    def test_unconfigured_origin_is_not_allowed(self) -> None:
        assert not is_allowed_origin("https://evil.example.com", self.ALLOWED)

    def test_absent_origin_header_is_allowed(self) -> None:
        """A request with no `Origin` is not a browser making a cross-origin
        request — it is a service-to-service caller or a test client. Refusing
        it would break every non-browser caller while stopping nothing."""
        assert is_allowed_origin(None, self.ALLOWED)

    def test_absent_origin_header_is_allowed_even_with_no_origins_configured(self) -> None:
        assert is_allowed_origin(None, ())

    def test_any_origin_is_refused_when_none_are_configured(self) -> None:
        assert not is_allowed_origin("https://app.example.com", ())

    def test_matching_is_exact_not_prefix(self) -> None:
        """A suffix match is how `https://app.example.com.attacker.test` comes
        to be treated as ours."""
        assert not is_allowed_origin("https://app.example.com.attacker.test", self.ALLOWED)
        assert not is_allowed_origin("https://app.example.com/", self.ALLOWED)

    def test_matching_is_case_sensitive_and_port_sensitive(self) -> None:
        assert not is_allowed_origin("http://localhost:5174", self.ALLOWED)
        assert not is_allowed_origin("HTTPS://APP.EXAMPLE.COM", self.ALLOWED)
