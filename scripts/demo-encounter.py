"""Drive one encounter end to end without AWS, by standing in for audio-ingestion.

    uv run python scripts/demo-encounter.py
    uv run python scripts/demo-encounter.py --session-id <uuid>   # attach to a UI visit

WHAT THIS IS FOR
----------------
The nudge spine — keyword detection, CPT resolution, the two-tier policy lookup,
the ``clinical_nudges`` write and the WebSocket relay into a browser — runs
entirely on local infrastructure. The one thing upstream of it that does not is
AWS Transcribe Medical, which has no local mock and is the only reason a
developer without AWS credentials cannot see any of it work.

So this script publishes a scripted transcript onto
``transcription:{session_id}`` itself. Everything downstream cannot tell the
difference: it is the same channel, the same payload shape and the same
stabilized-results-only discipline that ``audio-ingestion`` publishes under.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
It does not mock, patch or replace any part of a service. It is a *producer* on
a bus that already has one, which is why it needs no test doubles and proves
something real about the services it feeds. A nudge raised by this script
travelled the whole path a nudge raised from a microphone travels.

It is also not a substitute for Bedrock, and with no credentials the nudge it
raises is the safe fallback: ``requires_auth`` true, no criteria, and "confirm
manually". That is the documented fallback answer rather than a failure of this
script, and it is the honest shape of the product with its reasoning model
unavailable.

**Note which tier does not rescue that, because it is counter-intuitive.** The
local Da Vinci CRD Reference Implementation needs no AWS and is running, so it
looks like it should still supply an authoritative ``requires_auth``. It does
not: CRD is specified as a *patient-specific* coverage check, our request carries
a placeholder subject by construction (CLAUDE.md, "The CRD request carries no
patient"), and the RI therefore answers "unable to process" rather than making a
determination. Verified by running it. Closing that is TASK-059, which is gated
on real ``Patient`` and ``Coverage`` resources — so until then, CRD contributes
nothing to a local demo and the criteria are Bedrock's alone to supply.

NO PHI
------
Every line of the scripted transcript below is invented for this script. It
describes no real person and no real encounter. The ``text`` field is treated as
PHI everywhere else in this repository regardless, so nothing here logs a
segment's content — the same rule ``publisher.py`` and both consumers follow.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import uuid
from dataclasses import dataclass
from typing import Final

import httpx
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# audio-ingestion's own publisher, imported rather than reimplemented. The wire
# shape is fixed in CLAUDE.md, "The transcript segment payload — one shape", and
# a second definition of it here is exactly what that section exists to prevent:
# this script's whole value is that what it publishes is indistinguishable from
# what the real producer publishes.
#
# The import is `src.*` because audio-ingestion still declares
# `packages = ["src"]`. Three services do, so they share one top-level `src` in
# the workspace venv and whichever sorts first wins — the hazard ADR-0002
# records. It resolves to audio-ingestion today; `_assert_publisher_is_audio_ingestion`
# below turns a future reshuffle into a named failure instead of a script that
# silently publishes some other service's idea of a segment.
from src.publisher import channel_for, encode_segment  # noqa: E402
from src.transcription import TranscriptSegment  # noqa: E402
from track_a_clinical.db import DatabaseConfigurationError, database_url
from track_a_clinical.models import Encounter, Provider

logger: Final = logging.getLogger("demo-encounter")

DEFAULT_TRACK_A_CLINICAL_URL: Final = "http://localhost:8003"
DEFAULT_REDIS_URL: Final = "redis://localhost:6379/0"

#: Seconds between published segments. Roughly conversational pacing, so a
#: watcher sees the transcript build the way a real one does rather than
#: arriving as one block. Override with --speed.
DEFAULT_SEGMENT_DELAY_SECONDS: Final = 2.0

#: The coverage this encounter is filed under. These three columns are what
#: `policy_dispatch` reads to build a policy query, and a SMART launch fills them
#: (TASK-052b). A session started outside a launch has them null, so this script
#: sets them — otherwise every mention raises `MissingQueryParameters` and no
#: nudge is ever reached.
#:
#: Blue Cross Blue Shield of Massachusetts specifically, and not the generic
#: `blue-cross-blue-shield` bucket, because that licensee is what the dev corpus
#: in `seed-policies.py` is ingested under. A query under any other payer
#: retrieves nothing and looks exactly like a payer we hold no policy for — the
#: failure `packages/payer-vocab` exists to make visible. The value is the
#: payer's own display spelling; normalisation to a slug happens at the query
#: path, never here.
DEMO_PAYER: Final = "Blue Cross Blue Shield of Massachusetts"
DEMO_PLAN_TYPE: Final = "commercial"
DEMO_STATE: Final = "MA"

#: The patient this demo visit is about. A placeholder identifier rather than one
#: of the Synthea patients in the local HAPI server, because nothing in the path
#: this script exercises resolves a patient: the policy query carries payer, plan
#: and state, and CLAUDE.md is explicit that the CRD request carries no patient
#: at all. Pointing it at a real Synthea id would imply a resolution that does
#: not happen.
DEMO_PATIENT_FHIR_ID: Final = "demo-patient-knee-mri"


@dataclass(frozen=True)
class ScriptedLine:
    """One utterance, and how long after the previous one it was said."""

    text: str
    #: Seconds from the start of the encounter, as Transcribe would report them.
    start_time: float
    end_time: float


#: A knee-pain consultation that ends in an imaging order.
#:
#: Written to reach the nudge and to make the nudge *correct*: the patient is
#: described as having had rest and ibuprofen, which is not the six weeks of
#: supervised conservative therapy a payer asks for before advanced imaging. So
#: the gap is real rather than manufactured, and `missing_criteria` comes back
#: non-empty when the RAG half is available to populate it.
#:
#: "knee MRI" resolves to CPT 73721 through `procedure_codes`, which is the code
#: the BCBSMA Carelon extremity imaging policy in the dev corpus covers.
SCRIPTED_ENCOUNTER: Final[tuple[ScriptedLine, ...]] = (
    ScriptedLine("Good morning, come on in and have a seat.", 0.0, 2.6),
    ScriptedLine(
        "So tell me what has been going on with the knee.",
        3.1,
        5.8,
    ),
    ScriptedLine(
        "It started about six weeks ago after I was moving boxes. The right one, on the inside.",
        6.4,
        12.2,
    ),
    ScriptedLine(
        "Has anything made it better? Have you been doing any physical therapy?",
        12.9,
        16.4,
    ),
    ScriptedLine(
        "Not formally, no. I rested it for a couple of weeks and I have been "
        "taking ibuprofen when it flares up.",
        17.0,
        23.1,
    ),
    ScriptedLine(
        "All right. There is tenderness along the medial joint line and "
        "a positive McMurray on that side.",
        23.8,
        29.9,
    ),
    ScriptedLine(
        "That pattern makes me think meniscus. Let us get a knee MRI so we can "
        "see the cartilage properly before we talk about anything else.",
        30.5,
        38.2,
    ),
    ScriptedLine(
        "Whatever you think is best, doctor.",
        38.8,
        40.6,
    ),
)


class DemoError(Exception):
    """Something the person running this script has to fix."""


def _assert_publisher_is_audio_ingestion() -> None:
    """Fail loudly if `src.publisher` stopped resolving to audio-ingestion.

    Three services install a top-level ``src`` into the shared workspace venv.
    If the winner ever changes, the import above would quietly bind some other
    module and this script would publish a payload no consumer understands —
    which reads as "the nudge pipeline is broken" rather than as an import
    resolving somewhere new. Name it instead.
    """
    from src import publisher

    path = str(getattr(publisher, "__file__", ""))
    if "audio-ingestion" not in path.replace("\\", "/"):
        raise DemoError(
            "`src.publisher` resolved to "
            f"{path or 'an unknown location'}, not to services/audio-ingestion. "
            'Three services declare packages = ["src"] and share one top-level '
            "module in the workspace venv (ADR-0002). Run this script from the "
            "repository root, or rename the shadowing package."
        )


async def _start_session(client: httpx.AsyncClient, provider_id: uuid.UUID) -> uuid.UUID:
    """Create an encounter through the real endpoint and return its session id.

    Deliberately over HTTP rather than by inserting an `encounters` row: this is
    the endpoint that mints the JWT, writes the audit row and publishes
    `sessions:started`, and that last one is what makes track-b-rag subscribe to
    the transcript channel at all. A row inserted behind the endpoint's back
    would leave nobody listening.
    """
    response = await client.post(
        "/sessions/start",
        json={
            "patient_id": DEMO_PATIENT_FHIR_ID,
            "provider_id": str(provider_id),
            "ehr_encounter_id": None,
        },
    )
    if response.status_code != 201:
        raise DemoError(
            f"POST /sessions/start answered {response.status_code}: {response.text[:300]}"
        )
    session_id = response.json()["data"]["session_id"]
    return uuid.UUID(session_id)


async def _end_session(client: httpx.AsyncClient, session_id: uuid.UUID) -> None:
    """End the encounter, which is what triggers SOAP generation and bundle assembly.

    Both of those need Bedrock, so with no AWS credentials this publishes the
    signal and the consumers fail their own way. The signal is still worth
    sending: it is what deletes the `procedure_seen:` set, so a re-run of this
    script against a fresh session behaves like a fresh visit.
    """
    response = await client.post(f"/sessions/{session_id}/end")
    if response.status_code != 200:
        logger.warning(
            "POST /sessions/%s/end answered %s — the encounter stays active",
            session_id,
            response.status_code,
        )
        return
    logger.info("Ended session %s", session_id)


async def _first_provider(session_factory: async_sessionmaker) -> uuid.UUID:
    """Return a provider to file the demo visit under.

    `encounters.provider_id` is what every audit row for this visit names as its
    actor, and what the note and nudge routes resolve back to. It is read from
    the `providers` table rather than invented, because a fabricated identity in
    an audit trail is the failure CLAUDE.md's null-over-fabrication rule exists
    to prevent — and unlike `actor_id`, this column is not nullable, so there is
    no honest null to fall back on.
    """
    async with session_factory() as session:
        row = (
            await session.execute(select(Provider.id).order_by(Provider.created_at).limit(1))
        ).first()
    if row is None:
        raise DemoError(
            "The `providers` table is empty, so there is no provider to file a visit "
            "under. Complete a SMART launch once (which resolves a practitioner "
            "through POST /providers/resolve), or run "
            "`uv run python scripts/seed-test-encounters.py`."
        )
    return row[0]


async def _ensure_coverage(session_factory: async_sessionmaker, session_id: uuid.UUID) -> None:
    """Fill the three coverage columns a policy query is built from, if they are empty.

    Left alone when already populated: a session started through the UI after a
    SMART launch has had these written from a real `Coverage` resource
    (TASK-052b), and overwriting that with the demo payer would quietly change
    which policy corpus answers.
    """
    async with session_factory() as session, session.begin():
        encounter = (
            await session.execute(select(Encounter).where(Encounter.session_id == session_id))
        ).scalar_one_or_none()
        if encounter is None:
            raise DemoError(f"No encounter for session {session_id}.")
        if encounter.insurance_payer:
            logger.info(
                "Encounter already carries coverage (%s / %s / %s) — leaving it alone",
                encounter.insurance_payer,
                encounter.insurance_plan_type,
                encounter.state,
            )
            return
        encounter.insurance_payer = DEMO_PAYER
        encounter.insurance_plan_type = DEMO_PLAN_TYPE
        encounter.state = DEMO_STATE
    logger.info("Filed the encounter under %s / %s / %s", DEMO_PAYER, DEMO_PLAN_TYPE, DEMO_STATE)


async def _publish_transcript(redis: Redis, session_id: uuid.UUID, *, delay_seconds: float) -> None:
    """Publish the scripted encounter, one stabilized segment at a time.

    `is_partial=False` on every segment, matching ADR-0027: audio-ingestion
    publishes only stabilized results, and a partial here would be dropped by
    both consumers anyway.
    """
    channel = channel_for(str(session_id))
    logger.info("Publishing %d segments to %s", len(SCRIPTED_ENCOUNTER), channel)

    for index, line in enumerate(SCRIPTED_ENCOUNTER, start=1):
        segment = TranscriptSegment(
            result_id=str(uuid.uuid4()),
            text=line.text,
            is_partial=False,
            start_time=line.start_time,
            end_time=line.end_time,
        )
        subscribers = await redis.publish(
            channel, encode_segment(segment, session_id=str(session_id))
        )
        # The count, never the content. `text` is PHI by this repository's own
        # definition and the character count is what every other module on this
        # path logs instead.
        logger.info(
            "segment %d/%d - %d characters, %d subscriber(s)",
            index,
            len(SCRIPTED_ENCOUNTER),
            len(line.text),
            subscribers,
        )
        if subscribers == 0 and index == 1:
            logger.warning(
                "Nobody is subscribed to %s. track-b-rag subscribes when it sees the "
                "session on `sessions:started`, so start it before the session rather "
                "than after.",
                channel,
            )
        await asyncio.sleep(delay_seconds)


async def run(args: argparse.Namespace) -> int:
    """Start or attach to a session, then play the scripted encounter into it."""
    _assert_publisher_is_audio_ingestion()

    try:
        engine = create_async_engine(database_url(), pool_pre_ping=True)
    except DatabaseConfigurationError as error:
        logger.error("%s", error)
        return 1
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    redis = Redis.from_url(args.redis_url)

    try:
        async with httpx.AsyncClient(base_url=args.track_a_clinical_url, timeout=15.0) as client:
            if args.session_id is None:
                provider_id = await _first_provider(session_factory)
                session_id = await _start_session(client, provider_id)
                logger.info("Started session %s", session_id)
            else:
                session_id = args.session_id
                logger.info("Attaching to session %s", session_id)

            await _ensure_coverage(session_factory, session_id)

            # Through the logger rather than print(), per the Python conventions
            # in CLAUDE.md. A session id is not PHI: it is a server-generated
            # UUID naming a visit, and every service on this path already logs
            # it.
            logger.info("session_id: %s", session_id)
            logger.info("note screen: http://localhost:5173/notes/%s", session_id)
            logger.info("nudge feed:  ws://localhost:8005/ws/nudges/%s", session_id)

            # A beat before the first segment, so a watcher who has just been
            # handed the session id has time to open something pointed at it.
            await asyncio.sleep(args.lead_in)
            await _publish_transcript(redis, session_id, delay_seconds=args.speed)

            if args.end_session:
                await _end_session(client, session_id)
    except DemoError as error:
        logger.error("%s", error)
        return 1
    finally:
        await redis.aclose()
        await engine.dispose()

    logger.info("Done. A nudge, if one was raised, is in `clinical_nudges` for this encounter.")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the command line."""
    parser = argparse.ArgumentParser(
        description="Play a scripted encounter onto the transcript bus, no AWS required.",
    )
    parser.add_argument(
        "--session-id",
        type=uuid.UUID,
        default=None,
        help=(
            "Attach to an existing session instead of starting one. Use the id a "
            "visit started in the web app is showing, to watch nudges land in that UI."
        ),
    )
    parser.add_argument(
        "--track-a-clinical-url",
        default=DEFAULT_TRACK_A_CLINICAL_URL,
        help=f"Session lifecycle service (default: {DEFAULT_TRACK_A_CLINICAL_URL}).",
    )
    parser.add_argument(
        "--redis-url",
        default=DEFAULT_REDIS_URL,
        help=f"Redis to publish onto (default: {DEFAULT_REDIS_URL}).",
    )
    parser.add_argument(
        "--speed",
        type=float,
        default=DEFAULT_SEGMENT_DELAY_SECONDS,
        help="Seconds between segments. 0 plays the whole encounter at once.",
    )
    parser.add_argument(
        "--lead-in",
        type=float,
        default=3.0,
        help="Seconds to wait before the first segment, to get a watcher attached.",
    )
    parser.add_argument(
        "--end-session",
        action="store_true",
        help="End the encounter afterwards, firing SOAP generation and bundle assembly.",
    )
    return parser.parse_args(argv)


def main() -> int:
    """Entry point."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    return asyncio.run(run(parse_args()))


if __name__ == "__main__":
    sys.exit(main())
