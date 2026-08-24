"""Synthetic encounter audio for the tests, generated rather than committed.

TASK-020's first acceptance test streams ten seconds of WAV audio. That audio is
built here instead of being checked in as a fixture file: a committed WAV would
be a binary blob nobody can review, and the only properties the tests care about
are its format and its length, both of which are clearer as code.

The audio is a quiet tone rather than pure zeroes. Silence is what a broken
capture path also produces, so a test that passed on zeroes would pass on a
client that had sent nothing at all.
"""

from __future__ import annotations

import io
import math
import struct
import wave
from collections.abc import Iterator
from typing import Final

#: What TASK-022 and TASK-023 capture, and what the service's defaults expect.
SAMPLE_RATE_HZ: Final = 16_000
SAMPLE_WIDTH_BYTES: Final = 2
CHANNELS: Final = 1

#: 250ms at the format above — the frame size both capture clients stream at.
CHUNK_BYTES: Final = SAMPLE_RATE_HZ * SAMPLE_WIDTH_BYTES // 4


def wav_bytes(*, seconds: float, frequency_hz: float = 220.0) -> bytes:
    """Return a complete WAV file: 16kHz, mono, signed 16-bit little-endian."""
    frames = bytearray()
    total_samples = int(SAMPLE_RATE_HZ * seconds)
    for index in range(total_samples):
        # Deliberately quiet — the amplitude is irrelevant, the point is that the
        # samples are not all zero.
        value = int(3000 * math.sin(2 * math.pi * frequency_hz * index / SAMPLE_RATE_HZ))
        frames += struct.pack("<h", value)

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(CHANNELS)
        writer.setsampwidth(SAMPLE_WIDTH_BYTES)
        writer.setframerate(SAMPLE_RATE_HZ)
        writer.writeframes(bytes(frames))
    return buffer.getvalue()


def pcm_payload(wav: bytes) -> bytes:
    """Return the raw PCM frames inside a WAV file, without the container header.

    Transcribe's ``pcm`` encoding means exactly this: headerless little-endian
    samples. A client that streamed the 44-byte RIFF header as if it were audio
    would have it transcribed as a fraction of a millisecond of noise, so the
    capture clients strip it and so does this helper.
    """
    with wave.open(io.BytesIO(wav), "rb") as reader:
        return reader.readframes(reader.getnframes())


def chunks(data: bytes, size: int = CHUNK_BYTES) -> Iterator[bytes]:
    """Split audio into the frames a client would send."""
    for start in range(0, len(data), size):
        yield data[start : start + size]
