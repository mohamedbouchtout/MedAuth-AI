"""get_db_session — the session it yields, and the rollback it guarantees.

No database is involved. What matters here is the contract around the session:
that the caller gets one, that a handler which raises mid-transaction does not
return a dirty connection to the pool, and that a handler which returns cleanly
is left to commit for itself.
"""

from __future__ import annotations

from types import TracebackType
from typing import Any

import pytest

from track_b_rag.api import dependencies
from track_b_rag.api.dependencies import get_db_session


class FakeSession:
    """Records whether it was rolled back and whether it was closed."""

    def __init__(self) -> None:
        self.rolled_back = False
        self.closed = False

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.closed = True

    async def rollback(self) -> None:
        self.rolled_back = True


@pytest.fixture
def session(monkeypatch: pytest.MonkeyPatch) -> FakeSession:
    fake = FakeSession()
    monkeypatch.setattr(dependencies, "get_sessionmaker", lambda: lambda: fake)
    return fake


async def test_it_yields_a_session(session: FakeSession) -> None:
    generator = get_db_session()

    assert await generator.__anext__() is session

    with pytest.raises(StopAsyncIteration):
        await generator.__anext__()


async def test_a_clean_handler_is_left_to_commit_for_itself(session: FakeSession) -> None:
    """Committing here would commit a handler's partial work behind its back."""
    generator = get_db_session()
    await generator.__anext__()

    with pytest.raises(StopAsyncIteration):
        await generator.__anext__()

    assert session.rolled_back is False


async def test_the_session_is_closed_either_way(session: FakeSession) -> None:
    generator = get_db_session()
    await generator.__anext__()

    with pytest.raises(StopAsyncIteration):
        await generator.__anext__()

    assert session.closed is True


async def test_a_raising_handler_rolls_back(session: FakeSession) -> None:
    """Without this the connection returns to the pool still inside a transaction."""
    generator = get_db_session()
    await generator.__anext__()

    with pytest.raises(RuntimeError):
        await generator.athrow(RuntimeError("the handler blew up"))

    assert session.rolled_back is True


async def test_the_handler_error_still_propagates(session: FakeSession) -> None:
    """Rolling back is cleanup, not handling — the request must still fail."""
    generator = get_db_session()
    await generator.__anext__()

    with pytest.raises(RuntimeError, match="the handler blew up"):
        await generator.athrow(RuntimeError("the handler blew up"))


async def test_it_uses_this_services_sessionmaker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guards against reaching for track_a_clinical's engine along with its models."""
    calls: list[Any] = []
    fake = FakeSession()

    def sessionmaker() -> Any:
        calls.append("called")
        return lambda: fake

    monkeypatch.setattr(dependencies, "get_sessionmaker", sessionmaker)

    generator = get_db_session()
    await generator.__anext__()
    with pytest.raises(StopAsyncIteration):
        await generator.__anext__()

    assert calls == ["called"]
