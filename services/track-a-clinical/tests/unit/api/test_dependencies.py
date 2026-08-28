"""How a route reaches the consumer the application lifespan owns."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from track_a_clinical.api.dependencies import get_transcript_consumer
from track_a_clinical.consumer import TranscriptConsumer


def request_for(app: FastAPI) -> Any:
    """A stand-in carrying only the attribute the dependency reads."""

    class FakeRequest:
        def __init__(self) -> None:
            self.app = app

    return FakeRequest()


async def test_the_running_consumer_is_returned() -> None:
    app = FastAPI()
    consumer = TranscriptConsumer(redis=None)  # type: ignore[arg-type]
    app.state.transcript_consumer = consumer

    assert await get_transcript_consumer(request_for(app)) is consumer


async def test_an_app_without_the_lifespan_has_none() -> None:
    """Most route tests build the app directly; /health reports that as error."""
    assert await get_transcript_consumer(request_for(FastAPI())) is None


async def test_something_else_on_app_state_is_not_mistaken_for_a_consumer() -> None:
    app = FastAPI()
    app.state.transcript_consumer = "not a consumer"

    assert await get_transcript_consumer(request_for(app)) is None
