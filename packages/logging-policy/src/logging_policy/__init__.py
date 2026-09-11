"""The repository's one third-party logging policy, installed by every service.

A service installs it first in ``create_app()``, beside the shared error
handlers and the CORS policy::

    from api_envelope import install_error_handlers
    from logging_policy import install_logging_policy

    def create_app() -> FastAPI:
        install_logging_policy()
        app = FastAPI(...)
        install_error_handlers(app)
        return app

A one-shot job has no app, so it calls the same function at the top of its
entry point instead — see ``policy_scraper.__main__``.

**The gap this closes (TASK-046).** ``httpx`` logs every request it makes at
INFO, as ``HTTP Request: GET <full url> "HTTP/1.1 200 OK"``. Nothing in this
repository configured that logger, so it inherited the root level and wrote
patient identifiers to stdout in the ordinary course of a working request —
against the first regulatory rule in CLAUDE.md. It is the whole URL and not only
the query string: ``get_patient()`` reads ``Patient/{patient_id}``, so a
sanitiser that scrubbed query parameters would have left the most ordinary read
in the tree untouched and looked like it had worked.

**Why a package and not one line in each ``main.py``.** Four services call out
over ``httpx`` and a fifth reaches ``botocore`` with an encounter's transcript,
so the one-line fix is the same line in five or six places — the duplication
this repository refuses, on the same terms as ``api-envelope``,
``session-auth`` and ``cors-policy``. More than that, what a library is allowed
to write is a platform-wide decision rather than one service's: a service that
configured its own would be free to answer it differently, and the answer that
matters is the one that holds everywhere.

**It is installed in every service, which is where it differs from
``cors-policy``.** That package goes only into services a browser reaches,
because middleware in a service no browser calls protects nothing. This one has
no such limit: every service's process can be turned up to DEBUG, and every one
of them links a library that writes request content at DEBUG. ``audio-ingestion``
is the sharpest case and calls no browser-facing route at all — its Comprehend
Medical call would put clinical text into ``botocore.endpoint``'s DEBUG line.

**Scope note:** this package decides the level of *third-party* loggers. It is
not a logging framework, it configures no handlers, sets no format, and does not
touch the root logger or any logger this repository owns — those stay with
``logging.getLogger(__name__)`` and whatever the service's runner configures,
per the Python conventions in CLAUDE.md. Raising the floor on a library is the
whole of its job, and the same boundary ``api-envelope``, ``session-auth`` and
``cors-policy`` each draw around themselves.

**What it deliberately leaves alone, and why that is not an oversight.**
``sqlalchemy.engine.Engine`` writes every statement and its bound parameters at
INFO, which is PHI, so it looks like the obvious sixth entry. It is not, and the
reason is mechanical: SQLAlchemy decides whether to emit those lines from the
engine's own ``echo`` flag rather than from the logger's level, through
``sqlalchemy.log.InstanceLogger``. Verified against SQLAlchemy 2.0.52 — with
``sqlalchemy.engine`` pinned to WARNING and ``echo=True``, every statement and
every bound parameter is still logged. An entry here would therefore be a claim
to protect something it cannot protect, which is worse than no entry at all.
What actually holds the line is that no engine in this repository passes
``echo``, and with it unset nothing is logged even with the root logger at
DEBUG. Keep it that way: enabling echo against a database holding PHI is the
decision to look at, not this table. ``asyncpg`` logs no queries at all.
"""

from logging_policy.policy import LIBRARY_LOG_FLOORS, install_logging_policy

__all__ = ["LIBRARY_LOG_FLOORS", "install_logging_policy"]
