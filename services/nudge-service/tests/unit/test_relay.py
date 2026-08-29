"""The relay's own decisions, below the route.

The route tests prove a nudge arrives at a client. These pin the two things that
would be easy to get wrong quietly: the channel name, and what happens to a
payload that cannot go into a text frame.
"""

from __future__ import annotations

import logging
import uuid

import pytest

from src import relay


class FakeRedis:
    def __init__(self, *, healthy: bool) -> None:
        self.healthy = healthy

    async def ping(self) -> bool:
        if not self.healthy:
            raise ConnectionError("redis unreachable")
        return True


class TestChannel:
    """The canonical key, formatted in one place."""

    def test_the_channel_matches_the_canonical_key_list(self) -> None:
        session_id = uuid.uuid4()

        assert relay.channel_for(session_id) == f"nudges:{session_id}"

    def test_the_channel_carries_no_wildcard(self) -> None:
        """Pattern-subscribing would hand one client every encounter."""
        assert "*" not in relay.channel_for(uuid.uuid4())


class TestDecoding:
    """What can be relayed, and what is dropped rather than crashing the socket."""

    def test_bytes_are_decoded_as_utf8(self) -> None:
        assert relay.decode_payload(b'{"denial_risk":"high"}') == '{"denial_risk":"high"}'

    def test_a_string_is_passed_through_unchanged(self) -> None:
        """redis-py hands back str when the client decodes responses itself."""
        assert relay.decode_payload('{"a":1}') == '{"a":1}'

    def test_nothing_about_the_payload_is_normalised(self) -> None:
        """The relay must not become a second definition of the nudge shape."""
        odd = '{"b":2,   "a":1}\n'

        assert relay.decode_payload(odd.encode()) == odd

    def test_a_non_utf8_payload_is_dropped_rather_than_raising(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A text frame carries UTF-8, so this one cannot be relayed at all.

        Dropping the single message beats letting a decode error tear down a live
        encounter's connection.
        """
        with caplog.at_level(logging.WARNING):
            assert relay.decode_payload(b"\xff\xfe not utf-8") is None

    def test_the_drop_is_logged_without_the_payload(self, caplog: pytest.LogCaptureFixture) -> None:
        """A nudge names a procedure and a patient's documentation gaps."""
        payload = "knee MRI missing conservative therapy"

        with caplog.at_level(logging.WARNING):
            relay.decode_payload(object())

        assert payload not in caplog.text

    def test_an_unexpected_type_is_dropped(self) -> None:
        assert relay.decode_payload(12345) is None


class TestMessageFiltering:
    """Subscribe confirmations share the connection with real messages."""

    def test_a_published_message_is_relayed(self) -> None:
        assert relay.is_nudge_message({"type": "message", "data": b"{}"}) is True

    @pytest.mark.parametrize("kind", ["subscribe", "unsubscribe", "psubscribe"])
    def test_a_subscription_confirmation_is_not(self, kind: str) -> None:
        assert relay.is_nudge_message({"type": kind, "data": 1}) is False

    def test_nothing_at_all_is_not_a_message(self) -> None:
        assert relay.is_nudge_message(None) is False


class TestHealth:
    async def test_a_reachable_bus_is_healthy(self) -> None:
        assert await relay.check_health(FakeRedis(healthy=True)) is True

    async def test_an_unreachable_bus_is_not(self) -> None:
        assert await relay.check_health(FakeRedis(healthy=False)) is False
