#
# Copyright (c) 2026, slng.ai
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Unit tests for SlngSTTService using a fake WebSocket."""

import json

import pytest
from pipecat.frames.frames import (
    InputAudioRawFrame,
    TranscriptionFrame,
    VADUserStoppedSpeakingFrame,
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
                {
                    "type": "final_transcript",
                    "transcript": "real text",
                    "confidence": 0.9,
                }
            ),
        ],
    )
    stt = _make_stt()

    down, _ = await run_test(
        stt,
        frames_to_send=[
            InputAudioRawFrame(
                audio=b"\x00\x00" * 160, sample_rate=16000, num_channels=1
            ),
            SleepFrame(sleep=0.3),
        ],
    )

    transcripts = [f for f in down if isinstance(f, TranscriptionFrame)]
    assert [t.text for t in transcripts] == ["real text"]


async def test_region_and_world_headers_sent(patch_ws):
    """region_override + world_part_override map to X-Region-Override / X-World-Part-Override."""
    fake = patch_ws("pipecat_slng.stt", [json.dumps({"type": "ready"})])
    stt = SlngSTTService(
        api_key="test-key",
        sample_rate=16000,
        region_override="eu-north-1",
        world_part_override="eu",
    )

    await run_test(stt, frames_to_send=[SleepFrame(sleep=0.1)])

    assert fake.connect_headers["X-Region-Override"] == "eu-north-1"
    assert fake.connect_headers["X-World-Part-Override"] == "eu"


async def test_provider_key_header_sent(patch_ws):
    """provider_key maps to the X-Slng-Provider-Key header (BYOK)."""
    fake = patch_ws("pipecat_slng.stt", [json.dumps({"type": "ready"})])
    stt = SlngSTTService(
        api_key="test-key",
        sample_rate=16000,
        provider_key="my-provider-key",
    )

    await run_test(stt, frames_to_send=[SleepFrame(sleep=0.1)])

    assert fake.connect_headers["X-Slng-Provider-Key"] == "my-provider-key"


async def test_provider_key_header_absent_by_default(patch_ws):
    """Without provider_key the BYOK header is never sent."""
    fake = patch_ws("pipecat_slng.stt", [json.dumps({"type": "ready"})])
    stt = _make_stt()

    await run_test(stt, frames_to_send=[SleepFrame(sleep=0.1)])

    assert "X-Slng-Provider-Key" not in fake.connect_headers


async def test_v19_connect_rejection_includes_server_body(monkeypatch):
    """A rejected WS upgrade surfaces the server response body, not just the status."""
    from websockets.datastructures import Headers
    from websockets.exceptions import InvalidStatus
    from websockets.http11 import Response

    body = b'{"error":"BYOK is only supported for external STT/TTS routes"}'
    rejection = InvalidStatus(Response(400, "Bad Request", Headers(), body))

    async def _reject(url, **kwargs):
        raise rejection

    monkeypatch.setattr("pipecat_slng.stt.websocket_connect", _reject)
    stt = _make_stt()

    pushed: list[str] = []

    async def _record_error(error_msg: str, exception: BaseException | None = None):
        pushed.append(error_msg)

    monkeypatch.setattr(stt, "push_error", _record_error)

    with pytest.raises(InvalidStatus):
        await stt._connect_websocket()

    assert pushed and "BYOK is only supported" in pushed[0]
    assert "HTTP 400" in pushed[0]


async def test_vad_stop_sends_finalize(patch_ws):
    """VADUserStoppedSpeakingFrame triggers a {type: finalize} send to the bridge."""
    fake = patch_ws("pipecat_slng.stt", [json.dumps({"type": "ready"})])
    stt = _make_stt()

    await run_test(
        stt,
        frames_to_send=[
            InputAudioRawFrame(
                audio=b"\x00\x00" * 160, sample_rate=16000, num_channels=1
            ),
            VADUserStoppedSpeakingFrame(),
            SleepFrame(sleep=0.2),
        ],
    )

    text_sends = [json.loads(s) for s in fake.sent if isinstance(s, str)]
    assert any(m.get("type") == "finalize" for m in text_sends)


async def test_from_finalize_confirms_finalize(patch_ws, monkeypatch):
    """A final_transcript with from_finalize=true calls confirm_finalize()."""
    patch_ws(
        "pipecat_slng.stt",
        [
            json.dumps({"type": "ready"}),
            json.dumps(
                {
                    "type": "final_transcript",
                    "transcript": "hello",
                    "from_finalize": True,
                }
            ),
        ],
    )
    stt = _make_stt()

    calls: list = []
    monkeypatch.setattr(stt, "confirm_finalize", lambda: calls.append("confirmed"))

    await run_test(
        stt,
        frames_to_send=[
            InputAudioRawFrame(
                audio=b"\x00\x00" * 160, sample_rate=16000, num_channels=1
            ),
            SleepFrame(sleep=0.3),
        ],
    )

    assert calls == ["confirmed"]


async def test_disconnect_sends_close(patch_ws):
    """On EndFrame the service sends {type: close} before tearing the socket down."""
    fake = patch_ws("pipecat_slng.stt", [json.dumps({"type": "ready"})])
    stt = _make_stt()

    await run_test(stt, frames_to_send=[SleepFrame(sleep=0.1)])

    text_sends = [json.loads(s) for s in fake.sent if isinstance(s, str)]
    assert any(m.get("type") == "close" for m in text_sends)
