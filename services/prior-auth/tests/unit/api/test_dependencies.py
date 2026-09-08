"""Request-scoped dependencies. The session one arrived with TASK-061's router.

What is worth asserting about it is the safety net rather than the happy path: a
handler that raises mid-transaction must not return a connection to the pool
still inside one, because the next request to borrow it inherits a transaction
nobody opened.
"""

from __future__ import annotations

import pytest

from prior_auth.api import dependencies


class FakeSession:
    """Records the lifecycle calls a failing handler should produce."""

    def __init__(self) -> None:
        self.rollbacks = 0
        self.closed = False

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self.closed = True


@pytest.fixture
def session(monkeypatch: pytest.MonkeyPatch) -> FakeSession:
    """Substitute the session factory so no database is needed."""
    fake = FakeSession()
    monkeypatch.setattr(dependencies, "get_sessionmaker", lambda: lambda: fake)
    return fake


async def test_a_session_is_yielded_and_closed(session: FakeSession) -> None:
    generator = dependencies.get_db_session()
    yielded = await anext(generator)

    assert yielded is session
    with pytest.raises(StopAsyncIteration):
        await anext(generator)
    assert session.closed is True
    assert session.rollbacks == 0


async def test_a_failing_handler_rolls_back(session: FakeSession) -> None:
    """Without this, the connection goes back to the pool mid-transaction."""
    generator = dependencies.get_db_session()
    await anext(generator)

    with pytest.raises(RuntimeError):
        await generator.athrow(RuntimeError("handler blew up"))

    assert session.rollbacks == 1
    assert session.closed is True


async def test_the_error_is_re_raised_rather_than_swallowed(session: FakeSession) -> None:
    """A rolled-back transaction that reported success would be worse than the
    failure it hid."""
    generator = dependencies.get_db_session()
    await anext(generator)

    with pytest.raises(ValueError, match="specific"):
        await generator.athrow(ValueError("specific"))


def test_the_session_factory_is_the_real_one() -> None:
    """Guards the monkeypatching above against a renamed factory."""
    from prior_auth import db

    assert dependencies.get_sessionmaker is db.get_sessionmaker


def test_the_dependency_is_what_the_route_declares() -> None:
    """A second session dependency would open a second transaction per request."""
    from prior_auth.api import submission

    assert submission.get_db_session is dependencies.get_db_session
