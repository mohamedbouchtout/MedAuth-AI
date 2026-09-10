"""Environment every test in this service can assume.

``create_app()`` validates the client return targets at startup (TASK-051f) and
refuses to boot without them, which is the whole point of validating there: the
alternative surfaces at the far end of an OAuth redirect chain, after a human has
logged in and a real credential has been spent. That means a bare ``create_app()``
in a test needs the same two variables a deployment does.

They are set here rather than in each test module so that adding a test does not
mean rediscovering why the app will not start. A test that is *about* the startup
check deletes them itself — see ``tests/unit/test_main.py``, which is where the
missing-and-malformed cases live.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from src.config import get_settings

#: A loopback web target, which is what Vite and docker compose actually serve.
TEST_WEB_RETURN_URL = "http://localhost:5173/launch"

#: A custom scheme, the shape the OS routes back to the app that opened the auth
#: session. Never http/https — see ``smart/delivery.py``.
TEST_MOBILE_RETURN_URI = "medauth://launch"


# Set at import time as well as per test, because ``src.main`` builds its app at
# module scope — so merely importing it, which collection does, already runs the
# startup check. A fixture cannot be early enough for that.
os.environ.setdefault("SMART_WEB_RETURN_URL", TEST_WEB_RETURN_URL)
os.environ.setdefault("SMART_MOBILE_RETURN_URI", TEST_MOBILE_RETURN_URI)


@pytest.fixture(autouse=True)
def _client_return_targets(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Configure the two return targets for every test that builds an app."""
    get_settings.cache_clear()
    monkeypatch.setenv("SMART_WEB_RETURN_URL", TEST_WEB_RETURN_URL)
    monkeypatch.setenv("SMART_MOBILE_RETURN_URI", TEST_MOBILE_RETURN_URI)
    yield
    get_settings.cache_clear()
