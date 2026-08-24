"""Amazon Transcribe **Medical** streaming, reached by patching the AWS SDK.

Read this before changing anything here or bumping ``amazon-transcribe``.

**What is being patched and why.** ``amazon-transcribe`` — the official AWS
Transcribe Streaming SDK for Python, pinned at 0.6.4 — implements
``StartStreamTranscription`` and nothing else. There is no
``start_medical_stream_transcription`` on its client, no ``specialty`` or
``type`` anywhere in the package, and its serializer writes the request URI
``/stream-transcription`` as a literal. ``boto3`` is not an alternative: it has
no streaming transcription API at all, only the batch job API, which is why this
service depends on the streaming SDK in the first place.

CLAUDE.md requires Transcribe *Medical*, and the difference is not cosmetic —
the medical model is what recognises drug names, dosages, anatomy and procedure
terms, which is exactly the vocabulary TASK-021's keyword scan and TASK-030's
SOAP generation consume downstream. Transcribing a clinical encounter with the
general model and calling it the same thing would quietly degrade every
consumer.

So this module reaches the medical operation by **subclassing the SDK's
serializer and client**. The wire difference between the two operations is small
and entirely in the request: a different URI and two extra headers. The response
side needs nothing — the medical stream emits the same
``:event-type: TranscriptEvent`` framing, and the SDK's parser reads every field
with a tolerant ``.get()``, so a ``MedicalResult`` (no ``ChannelId``, an extra
``Entities``) parses through the existing path unchanged.

**This is a patch of another project's internals, not configuration of it.**
``TranscribeStreamingSerializer.serialize_start_stream_transcription_request``
and ``TranscribeStreamingClient._serializer`` are not a published extension
point, and AWS has not shipped a release of this library since 0.6.4. What we
accept in exchange, deliberately:

* The dependency is pinned to ``==0.6.4`` in ``pyproject.toml`` rather than
  given a floor. A Dependabot bump here is a change to review, not a routine
  one.
* ``tests/unit/test_transcribe_medical.py`` asserts the *serialized request* —
  the URI and both headers — rather than trusting the subclass to have taken
  effect. If a future version restructures serialization so the override stops
  being called, that test fails rather than the service silently transcribing a
  clinical encounter with the general model.
* If AWS ever adds medical support upstream, delete the serializer and client
  below and call the real method. Nothing outside :mod:`src.transcription`'s
  seam depends on either of them.

**No PHI is logged here.** Transcript text passes through ``_to_segments`` on
its way to Redis and never reaches a log line, and neither do the audio bytes
going the other way.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any, Final

from amazon_transcribe.client import TranscribeStreamingClient
from amazon_transcribe.model import (
    StartStreamTranscriptionRequest,
    TranscriptEvent,
)
from amazon_transcribe.request import Request
from amazon_transcribe.serialize import TranscribeStreamingSerializer

from src.config import Settings
from src.transcription import TranscriptSegment

logger = logging.getLogger(__name__)

#: The medical operation's request URI. The SDK's serializer hardcodes
#: ``/stream-transcription``; this is the difference that selects the medical
#: model.
MEDICAL_REQUEST_URI: Final = "/medical-stream-transcription"

#: Header carrying the medical specialty (``PRIMARYCARE``, ``CARDIOLOGY``, …).
SPECIALTY_HEADER: Final = "x-amzn-transcribe-specialty"

#: Header carrying the encounter type — ``CONVERSATION`` for a clinician talking
#: with a patient, ``DICTATION`` for a clinician narrating alone.
TYPE_HEADER: Final = "x-amzn-transcribe-type"


class MedicalStreamSerializer(TranscribeStreamingSerializer):
    """Serializer that re-points the request at the medical operation.

    Everything the base class writes — language code, sample rate, encoding, the
    required ``host`` and content headers — is correct for the medical operation
    too. So this delegates and then amends, rather than reimplementing a long
    method against a private API that may move under us.
    """

    def __init__(self, *, specialty: str, type_: str) -> None:
        self._specialty = specialty
        self._type = type_

    def serialize_start_stream_transcription_request(
        self,
        endpoint: str,
        request_shape: StartStreamTranscriptionRequest,
    ) -> Request:
        """Return the base request, re-pointed at the medical endpoint.

        Amending ``path`` and ``headers`` before the caller calls ``prepare()``
        is what makes this work: SigV4 signs the prepared request, so the
        signature covers the medical URI and both medical headers. Amending
        after signing would produce a request the service rejects.
        """
        request = super().serialize_start_stream_transcription_request(
            endpoint=endpoint,
            request_shape=request_shape,
        )
        request.path = MEDICAL_REQUEST_URI
        request.headers[SPECIALTY_HEADER] = self._specialty
        request.headers[TYPE_HEADER] = self._type
        return request


class TranscribeMedicalStreamingClient(TranscribeStreamingClient):
    """The SDK client with its serializer swapped for the medical one.

    ``start_stream_transcription`` is inherited unchanged and starts a *medical*
    stream, because everything operation-specific about the request lives in the
    serializer it delegates to.
    """

    def __init__(self, *, region: str, specialty: str, type_: str) -> None:
        super().__init__(region=region)
        self._serializer = MedicalStreamSerializer(specialty=specialty, type_=type_)


def _to_segments(event: TranscriptEvent) -> list[TranscriptSegment]:
    """Map one Transcribe event onto this service's segment shape.

    Results carrying no alternative, or an empty one, are dropped: Transcribe
    emits them around silence, and an empty transcript is not something any
    downstream consumer can act on.
    """
    segments: list[TranscriptSegment] = []
    for result in event.transcript.results or []:
        alternatives = result.alternatives or []
        if not alternatives:
            continue
        text = alternatives[0].transcript
        if not text:
            continue
        segments.append(
            TranscriptSegment(
                result_id=str(result.result_id),
                text=text,
                is_partial=bool(result.is_partial),
                start_time=result.start_time,
                end_time=result.end_time,
            )
        )
    return segments


class TranscribeMedicalStream:
    """One live medical transcription, adapted to the service's seam.

    Structurally satisfies :class:`src.transcription.TranscriptionStream`.
    """

    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self._input_ended = False

    async def send_audio(self, chunk: bytes) -> None:
        """Enqueue a chunk of PCM audio for transcription."""
        await self._stream.input_stream.send_audio_event(audio_chunk=chunk)

    async def end_input(self) -> None:
        """Close the audio half of the stream so the result iterator can finish.

        Guarded because the route calls it on the normal path and ``close``
        calls it again during teardown; ending an already-ended stream writes to
        a closed buffer.
        """
        if self._input_ended:
            return
        self._input_ended = True
        await self._stream.input_stream.end_stream()

    async def close(self) -> None:
        """Release the stream, ending the audio half if the route did not."""
        try:
            await self.end_input()
        except Exception:
            # A stream the service already tore down raises on end. The
            # connection is over either way, and the route's own failure — if it
            # had one — is the error worth surfacing, not this one.
            logger.warning("Transcribe stream did not close cleanly", exc_info=True)

    async def segments(self) -> AsyncIterator[TranscriptSegment]:
        """Yield segments until the service closes the result stream."""
        async for event in self._stream.output_stream:
            if isinstance(event, TranscriptEvent):
                for segment in _to_segments(event):
                    yield segment


async def open_medical_stream(settings: Settings) -> TranscribeMedicalStream:
    """Start a Transcribe Medical stream configured from the environment.

    A client per connection rather than a shared one: the SDK client owns an AWS
    CRT event loop and a session manager bound to it, and 0.6.4 documents nothing
    about sharing that across concurrent streams.
    """
    client = TranscribeMedicalStreamingClient(
        region=settings.aws_region,
        specialty=settings.transcribe_medical_specialty,
        type_=settings.transcribe_medical_type,
    )
    stream = await client.start_stream_transcription(
        language_code=settings.transcribe_medical_language_code,
        media_sample_rate_hz=settings.transcribe_medical_sample_rate_hz,
        media_encoding=settings.transcribe_medical_media_encoding,
    )
    logger.info(
        "Opened Transcribe Medical stream (specialty=%s, type=%s, %dHz %s)",
        settings.transcribe_medical_specialty,
        settings.transcribe_medical_type,
        settings.transcribe_medical_sample_rate_hz,
        settings.transcribe_medical_media_encoding,
    )
    return TranscribeMedicalStream(stream)
