"""Teardown paths that tolerate a peer or a subscription already gone.

These run on every path out of the handler, including the failing ones, and each
exists to stop a secondary error replacing the failure actually being reported.
They are exercised directly because provoking them through the route would mean
faking a partially torn-down connection, which tests the fake rather than the
code.
"""

from __future__ import annotations

import logging

import pytest

from src.api.websocket import _close_quietly, _release_quietly


class ExplodingPubSub:
    """A subscription whose connection has already gone away."""

    def __init__(self, *, unsubscribe_fails: bool, close_fails: bool) -> None:
        self.unsubscribe_fails = unsubscribe_fails
        self.close_fails = close_fails
        self.unsubscribed = False
        self.closed = False

    async def unsubscribe(self, _channel: str) -> None:
        self.unsubscribed = True
        if self.unsubscribe_fails:
            raise ConnectionError("connection already gone")

    async def aclose(self) -> None:
        self.closed = True
        if self.close_fails:
            raise ConnectionError("connection already gone")


class ClosedSocket:
    """A socket the client already disconnected from."""

    def __init__(self) -> None:
        self.close_attempts = 0

    async def close(self, code: int) -> None:
        self.close_attempts += 1
        raise RuntimeError(f"cannot close({code}), already disconnected")


async def test_a_failing_unsubscribe_still_closes_the_subscription() -> None:
    """The close must not be skipped because the unsubscribe raised."""
    pubsub = ExplodingPubSub(unsubscribe_fails=True, close_fails=False)

    await _release_quietly(pubsub, "nudges:abc")

    assert pubsub.unsubscribed is True
    assert pubsub.closed is True


async def test_a_failing_close_is_swallowed() -> None:
    pubsub = ExplodingPubSub(unsubscribe_fails=False, close_fails=True)

    await _release_quietly(pubsub, "nudges:abc")

    assert pubsub.closed is True


async def test_neither_failure_is_logged_with_nudge_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Teardown logs name the situation, never what crossed the socket."""
    with caplog.at_level(logging.DEBUG):
        await _release_quietly(
            ExplodingPubSub(unsubscribe_fails=True, close_fails=True), "nudges:x"
        )

    assert "knee MRI" not in caplog.text


async def test_closing_an_already_disconnected_socket_does_not_raise() -> None:
    """That exception would replace the failure being reported with a worse one."""
    socket = ClosedSocket()

    await _close_quietly(socket, 1011)

    assert socket.close_attempts == 1
