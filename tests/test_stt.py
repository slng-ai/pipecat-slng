#
# Copyright (c) 2026, slng.ai
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Unit tests for SlngSTTService using a fake WebSocket."""

import json

from pipecat.frames.frames import (
    InputAudioRawFrame,
    TranscriptionFrame,
)
from pipecat.tests.utils import SleepFrame, run_test

from pipecat_slng import SlngSTTService


def _make_stt():
    return SlngSTTService(api_key="test-key", sample_rate=16000)


async def test_init_message_sent_on_start(patch_ws):
    """Service sends an init message with config after connecting."""
    fake = patch_ws("pipecat_slng.stt", [json.dumps({"type": "ready"})])
    stt = _make_stt()

    await run_test(
        stt,
        frames_to_send=[SleepFrame(sleep=0.1)],
    )

    text_sends = [json.loads(s) for s in fake.sent if isinstance(s, str)]
    init = next(m for m in text_sends if m.get("type") == "init")
    assert init["config"]["sample_rate"] == 16000
    assert init["config"]["encoding"] == "linear16"


async def test_auth_header_sent(patch_ws):
    """Bearer token is passed as an Authorization header."""
    fake = patch_ws("pipecat_slng.stt", [json.dumps({"type": "ready"})])
    stt = _make_stt()

    await run_test(stt, frames_to_send=[SleepFrame(sleep=0.1)])

    assert fake.connect_headers["Authorization"] == "Bearer test-key"
    assert "/v1/bridges/unmute/stt/" in fake.connect_url


async def test_final_transcript_emits_transcription_frame(patch_ws):
    """A final_transcript server frame becomes a TranscriptionFrame."""
    patch_ws(
        "pipecat_slng.stt",
        [
            json.dumps({"type": "ready"}),
            json.dumps({"type": "final_transcript", "transcript": "hello world"}),
        ],
    )
    stt = _make_stt()

    down, _ = await run_test(
        stt,
        frames_to_send=[
            InputAudioRawFrame(
                audio=b"\x00\x00" * 160, sample_rate=16000, num_channels=1
            ),
            SleepFrame(sleep=0.2),
        ],
    )

    transcripts = [f for f in down if isinstance(f, TranscriptionFrame)]
    assert transcripts[0].text == "hello world"


async def test_audio_sent_as_binary(patch_ws):
    """Raw audio bytes are forwarded to the server as a binary frame."""
    fake = patch_ws("pipecat_slng.stt", [json.dumps({"type": "ready"})])
    stt = _make_stt()
    audio = b"\x01\x02" * 160

    await run_test(
        stt,
        frames_to_send=[
            InputAudioRawFrame(audio=audio, sample_rate=16000, num_channels=1),
            SleepFrame(sleep=0.2),
        ],
    )

    assert any(isinstance(s, bytes) and s == audio for s in fake.sent)


async def test_low_confidence_transcript_dropped(patch_ws):
    """A final_transcript with confidence < 0.5 is suppressed (community guide)."""
    patch_ws(
        "pipecat_slng.stt",
        [
            json.dumps({"type": "ready"}),
            json.dumps(
                {"type": "final_transcript", "transcript": "noise", "confidence": 0.3}
            ),
            json.dumps(
                {"type": "final_transcript", "transcript": "real text", "confidence": 0.9}
            ),
        ],
    )
    stt = _make_stt()

    down, _ = await run_test(
        stt,
        frames_to_send=[
            InputAudioRawFrame(audio=b"\x00\x00" * 160, sample_rate=16000, num_channels=1),
            SleepFrame(sleep=0.3),
        ],
    )

    transcripts = [f for f in down if isinstance(f, TranscriptionFrame)]
    assert [t.text for t in transcripts] == ["real text"]
