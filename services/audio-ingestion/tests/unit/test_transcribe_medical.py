"""What the SDK patch must keep doing for the medical model to be reached.

:mod:`src.transcribe_medical` subclasses ``amazon-transcribe``'s serializer and
client to reach an operation the library does not implement. The interesting
failure mode is silent: if a future version restructures serialization so the
override stops taking effect, every request still succeeds — against the
*general* transcription model, quietly losing the clinical vocabulary the whole
service exists to capture.

So these tests assert the serialized request itself, not that the subclass was
constructed. They are what a Dependabot bump of the pinned ``==0.6.4`` has to
pass.
"""

from __future__ import annotations

from amazon_transcribe.model import (
    Alternative,
    Result,
    StartStreamTranscriptionRequest,
    Transcript,
    TranscriptEvent,
)

import src.transcribe_medical as module
from src.config import Settings
from src.transcribe_medical import (
    MEDICAL_REQUEST_URI,
    SPECIALTY_HEADER,
    TYPE_HEADER,
    MedicalStreamSerializer,
    TranscribeMedicalStream,
    TranscribeMedicalStreamingClient,
    _to_segments,
)

ENDPOINT = "https://transcribestreaming.us-east-1.amazonaws.com"


def serialized() -> object:
    """Serialize a representative request through the medical serializer."""
    serializer = MedicalStreamSerializer(specialty="PRIMARYCARE", type_="CONVERSATION")
    shape = StartStreamTranscriptionRequest(
        language_code="en-US",
        media_sample_rate_hz=16_000,
        media_encoding="pcm",
    )
    return serializer.serialize_start_stream_transcription_request(
        endpoint=ENDPOINT,
        request_shape=shape,
    )


def test_the_request_targets_the_medical_operation() -> None:
    """The URI is what selects Transcribe Medical over general transcription."""
    assert serialized().path == MEDICAL_REQUEST_URI  # type: ignore[attr-defined]


def test_specialty_and_encounter_type_are_sent_as_headers() -> None:
    """Without these the medical endpoint rejects the request outright."""
    headers = serialized().headers  # type: ignore[attr-defined]

    assert headers[SPECIALTY_HEADER] == "PRIMARYCARE"
    assert headers[TYPE_HEADER] == "CONVERSATION"


def test_the_base_serializer_still_supplies_the_audio_format() -> None:
    """The override amends the base request rather than replacing it.

    If a bump changed the base header names, this fails here rather than as a
    stream that hangs — Transcribe's response to a sample rate it did not
    receive is silence, not an error.
    """
    headers = serialized().headers  # type: ignore[attr-defined]

    assert headers["x-amzn-transcribe-language-code"] == "en-US"
    assert headers["x-amzn-transcribe-sample-rate"] == "16000"
    assert headers["x-amzn-transcribe-media-encoding"] == "pcm"


def test_the_client_serializes_through_the_medical_serializer() -> None:
    """The client's own hook is what carries the patch into the request path.

    Constructing the client resolves AWS credentials lazily and opens nothing,
    so this needs no network and no account.
    """
    client = TranscribeMedicalStreamingClient(
        region="us-east-1",
        specialty="CARDIOLOGY",
        type_="DICTATION",
    )

    assert isinstance(client._serializer, MedicalStreamSerializer)


def test_the_specialty_reaches_the_request_from_the_client() -> None:
    """End to end through the two subclasses, with no request actually sent."""
    client = TranscribeMedicalStreamingClient(
        region="us-east-1",
        specialty="CARDIOLOGY",
        type_="DICTATION",
    )
    request = client._serializer.serialize_start_stream_transcription_request(
        endpoint=ENDPOINT,
        request_shape=StartStreamTranscriptionRequest(
            language_code="en-US",
            media_sample_rate_hz=16_000,
            media_encoding="pcm",
        ),
    )

    assert request.path == MEDICAL_REQUEST_URI
    assert request.headers[SPECIALTY_HEADER] == "CARDIOLOGY"
    assert request.headers[TYPE_HEADER] == "DICTATION"


def event(*results: Result) -> TranscriptEvent:
    return TranscriptEvent(Transcript(list(results)))


def result(
    *,
    result_id: str = "r1",
    text: str | None = "an MRI of the left knee",
    is_partial: bool = False,
) -> Result:
    alternatives = [Alternative(text, [], None)] if text is not None else []
    return Result(
        result_id=result_id,
        start_time=1.0,
        end_time=2.5,
        is_partial=is_partial,
        alternatives=alternatives,
        channel_id=None,
    )


def test_a_result_maps_onto_the_services_segment_shape() -> None:
    segments = _to_segments(event(result()))

    assert len(segments) == 1
    assert segments[0].result_id == "r1"
    assert segments[0].text == "an MRI of the left knee"
    assert segments[0].is_partial is False
    assert segments[0].start_time == 1.0
    assert segments[0].end_time == 2.5


def test_results_carrying_no_usable_transcript_are_dropped() -> None:
    """Transcribe emits these around silence; nothing downstream can use them."""
    segments = _to_segments(
        event(
            result(result_id="empty-alternatives", text=None),
            result(result_id="empty-text", text=""),
            result(result_id="real"),
        )
    )

    assert [segment.result_id for segment in segments] == ["real"]


def test_partial_results_are_mapped_and_labelled_rather_than_dropped() -> None:
    """Dropping partials is the publisher's decision, not this mapper's.

    Keeping the distinction here means the rule lives in exactly one place, and
    a later task that wants earlier signal can change that place alone.
    """
    segments = _to_segments(event(result(is_partial=True)))

    assert [segment.is_partial for segment in segments] == [True]


class FakeInputStream:
    """The SDK's audio input half, recording what the adapter pushes at it."""

    def __init__(self, *, fail_on_end: bool = False) -> None:
        self.chunks: list[bytes] = []
        self.ends = 0
        self.fail_on_end = fail_on_end

    async def send_audio_event(self, audio_chunk: bytes) -> None:
        self.chunks.append(audio_chunk)

    async def end_stream(self) -> None:
        self.ends += 1
        if self.fail_on_end:
            raise RuntimeError("stream already torn down by the service")


class FakeSdkStream:
    """Stands in for ``StartStreamTranscriptionEventStream``."""

    def __init__(self, events: list[object], *, fail_on_end: bool = False) -> None:
        self.input_stream = FakeInputStream(fail_on_end=fail_on_end)
        self._events = events

    @property
    def output_stream(self) -> object:
        async def iterate():  # type: ignore[no-untyped-def]
            for item in self._events:
                yield item

        return iterate()


async def test_audio_is_forwarded_to_the_sdk_stream() -> None:
    sdk = FakeSdkStream([])
    stream = TranscribeMedicalStream(sdk)

    await stream.send_audio(b"pcm-bytes")

    assert sdk.input_stream.chunks == [b"pcm-bytes"]


async def test_ending_the_input_twice_only_ends_it_once() -> None:
    """The route ends the input, then teardown ends it again.

    Without the guard the second call writes to a closed buffer.
    """
    sdk = FakeSdkStream([])
    stream = TranscribeMedicalStream(sdk)

    await stream.end_input()
    await stream.close()

    assert sdk.input_stream.ends == 1


async def test_closing_an_unended_stream_ends_it() -> None:
    """Teardown after a failure that skipped ``end_input`` still releases it."""
    sdk = FakeSdkStream([])
    await TranscribeMedicalStream(sdk).close()

    assert sdk.input_stream.ends == 1


async def test_a_failure_during_close_is_logged_rather_than_raised() -> None:
    """The route's own failure is the one worth surfacing, not this one."""
    sdk = FakeSdkStream([], fail_on_end=True)

    await TranscribeMedicalStream(sdk).close()  # must not raise


async def test_only_transcript_events_become_segments() -> None:
    """The result stream carries other event types; they are not transcripts."""
    sdk = FakeSdkStream([object(), event(result()), object()])

    segments = [segment async for segment in TranscribeMedicalStream(sdk).segments()]

    assert [segment.result_id for segment in segments] == ["r1"]


async def test_opening_a_stream_uses_the_configured_specialty(
    monkeypatch: object,
) -> None:
    """``open_medical_stream`` wires settings into the client, opening no socket."""
    captured: dict[str, object] = {}

    class FakeClient:
        def __init__(self, *, region: str, specialty: str, type_: str) -> None:
            captured["region"] = region
            captured["specialty"] = specialty
            captured["type"] = type_

        async def start_stream_transcription(self, **kwargs: object) -> FakeSdkStream:
            captured.update(kwargs)
            return FakeSdkStream([])

    monkeypatch.setattr(module, "TranscribeMedicalStreamingClient", FakeClient)  # type: ignore[attr-defined]

    settings = Settings(
        jwt_signing_key="open-stream-test-signing-key-32byt",
        aws_region="us-east-1",
        transcribe_medical_specialty="CARDIOLOGY",
        transcribe_medical_type="DICTATION",
    )
    stream = await module.open_medical_stream(settings)

    assert isinstance(stream, module.TranscribeMedicalStream)
    assert captured["specialty"] == "CARDIOLOGY"
    assert captured["type"] == "DICTATION"
    assert captured["media_sample_rate_hz"] == 16_000
    assert captured["media_encoding"] == "pcm"
