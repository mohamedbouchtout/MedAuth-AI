"""The relay's own decisions, below the route.

The route tests prove a message arrives at a client. These pin the two things
that would be easy to get wrong quietly: the channel names, and what happens to a
payload that cannot go into a text frame.

These helpers are channel-neutral as of TASK-041d, which is why the channel is an
argument here rather than baked in. The templates are asserted against CLAUDE.md's
canonical key list separately from the formatting, so a variant spelling of either
key fails on its own.
"""

from __future__ import annotations

import logging
import uuid

import pytest

from src import relay

#: Any channel name, for the decoding tests, which do not care which stream a
#: payload came from beyond putting it in the log line.
SOME_CHANNEL = "nudges:11111111-1111-1111-1111-111111111111"


class FakeRedis:
    def __init__(self, *, healthy: bool) -> None:
        self.healthy = healthy

    async def ping(self) -> bool:
        if not self.healthy:
            raise ConnectionError("redis unreachable")
        return True


class TestChannels:
    """The canonical keys, formatted in one place."""

    def test_the_nudge_channel_matches_the_canonical_key_list(self) -> None:
        session_id = uuid.uuid4()

        assert relay.channel_for(relay.NUDGE_CHANNEL_TEMPLATE, session_id) == f"nudges:{session_id}"

    def test_the_transcript_channel_matches_the_canonical_key_list(self) -> None:
        session_id = uuid.uuid4()

        assert (
            relay.channel_for(relay.TRANSCRIPT_CHANNEL_TEMPLATE, session_id)
            == f"transcription:{session_id}"
        )

    @pytest.mark.parametrize(
        "template",
        [relay.NUDGE_CHANNEL_TEMPLATE, relay.TRANSCRIPT_CHANNEL_TEMPLATE],
    )
    def test_no_channel_carries_a_wildcard(self, template: str) -> None:
        """Pattern-subscribing would hand one client every encounter."""
        assert "*" not in relay.channel_for(template, uuid.uuid4())

    def test_the_two_streams_are_not_the_same_channel(self) -> None:
        """A copy-paste of one template into the other would be silent."""
        assert relay.NUDGE_CHANNEL_TEMPLATE != relay.TRANSCRIPT_CHANNEL_TEMPLATE


class TestDecoding:
    """What can be relayed, and what is dropped rather than crashing the socket."""

    def test_bytes_are_decoded_as_utf8(self) -> None:
        assert (
            relay.decode_payload(b'{"denial_risk":"high"}', channel=SOME_CHANNEL)
            == '{"denial_risk":"high"}'
        )

    def test_a_string_is_passed_through_unchanged(self) -> None:
        """redis-py hands back str when the client decodes responses itself."""
        assert relay.decode_payload('{"a":1}', channel=SOME_CHANNEL) == '{"a":1}'

    def test_nothing_about_the_payload_is_normalised(self) -> None:
        """The relay must not become a second definition of either payload shape."""
        odd = '{"b":2,   "a":1}\n'

        assert relay.decode_payload(odd.encode(), channel=SOME_CHANNEL) == odd

    def test_a_non_utf8_payload_is_dropped_rather_than_raising(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A text frame carries UTF-8, so this one cannot be relayed at all.

        Dropping the single message beats letting a decode error tear down a live
        encounter's connection.
        """
        with caplog.at_level(logging.WARNING):
            assert relay.decode_payload(b"\xff\xfe not utf-8", channel=SOME_CHANNEL) is None

    def test_the_drop_names_the_channel_it_happened_on(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """With two streams relayed, a drop that names neither is not actionable."""
        channel = "transcription:22222222-2222-2222-2222-222222222222"

        with caplog.at_level(logging.WARNING):
            relay.decode_payload(b"\xff\xfe", channel=channel)

        assert channel in caplog.text

    def test_the_drop_is_logged_without_the_payload(self, caplog: pytest.LogCaptureFixture) -> None:
        """Transcript text is what was said in an encounter; a nudge names a gap."""
        payload = "patient reports right knee pain for six weeks"

        with caplog.at_level(logging.WARNING):
            relay.decode_payload(payload, channel=SOME_CHANNEL)
            relay.decode_payload(object(), channel=SOME_CHANNEL)

        assert payload not in caplog.text

    def test_an_unexpected_type_is_dropped(self) -> None:
        assert relay.decode_payload(12345, channel=SOME_CHANNEL) is None


class TestMessageFiltering:
    """Subscribe confirmations share the connection with real messages."""

    def test_a_published_message_is_relayed(self) -> None:
        assert relay.is_published_message({"type": "message", "data": b"{}"}) is True

    @pytest.mark.parametrize("kind", ["subscribe", "unsubscribe", "psubscribe"])
    def test_a_subscription_confirmation_is_not(self, kind: str) -> None:
        assert relay.is_published_message({"type": kind, "data": 1}) is False

    def test_nothing_at_all_is_not_a_message(self) -> None:
        assert relay.is_published_message(None) is False


class TestHealth:
    async def test_a_reachable_bus_is_healthy(self) -> None:
        assert await relay.check_health(FakeRedis(healthy=True)) is True

    async def test_an_unreachable_bus_is_not(self) -> None:
        assert await relay.check_health(FakeRedis(healthy=False)) is False
