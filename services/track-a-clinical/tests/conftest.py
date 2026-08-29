"""Suite-wide environment bootstrap.

``track_a_clinical.main`` builds its application at import time, and since
TASK-041c ``create_app()`` reads ``Settings`` to configure CORS. ``Settings``
has exactly one field with no default — ``JWT_SIGNING_KEY`` — so importing the
module without it raises before any fixture can run, and a conftest that merely
provided a fixture would be too late.

Requiring the variable is the correct production behaviour: a service that
cannot mint session tokens should fail at startup with a message naming the
missing variable, not on the first request. What that costs is this file.

The value here is a placeholder for tests that never touch a token. Anything
testing signing behaviour sets its own key with ``monkeypatch.setenv``, and
``tests/unit/test_config.py`` still removes the variable with
``monkeypatch.delenv`` to assert the settings refuse to construct without it —
monkeypatch unsets it regardless of what is set here.
"""

from __future__ import annotations

import os

#: 34 characters, over the 32-byte floor the issuer enforces.
os.environ.setdefault("JWT_SIGNING_KEY", "track-a-suite-default-key-32bytes")
