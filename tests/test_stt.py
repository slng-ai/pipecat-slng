#
# Copyright (c) 2026, slng.ai
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Unit tests for SlngSTTService using a fake WebSocket."""

import asyncio
import json

import pytest
from pipecat.frames.frames import (
    InputAudioRawFrame,
    InterimTranscriptionFrame,
    InterruptionFrame,
    TranscriptionFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.tests.utils import SleepFrame, run_test

from pipecat_slng import SlngSTTService


def _make_stt():
    return SlngSTTService(api_key="test-key", sample_rate=16000)


def _audio():
    """One 10ms frame of silence — enough to open the stream."""
    return InputAudioRawFrame(
        audio=b"\x00\x00" * 160, sample_rate=16000, num_channels=1
    )


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
            _audio(),
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


async def test_low_confidence_final_is_still_emitted(patch_ws):
    """A low-confidence final_transcript is never dropped.

    Dropping it hangs the turn: _maybe_trigger_user_turn_stopped returns early
    with no text (turn_analyzer_user_turn_stop_strategy.py:350) and the timeout
    handler routes through the same check, so the turn never stops until some
    later transcript arrives.
    """
    patch_ws(
        "pipecat_slng.stt",
        [
            json.dumps({"type": "ready"}),
            json.dumps(
                {"type": "final_transcript", "transcript": "noise", "confidence": 0.3}
            ),
        ],
    )
    stt = _make_stt()

    down, _ = await run_test(
        stt,
        frames_to_send=[
            _audio(),
            SleepFrame(sleep=0.3),
        ],
    )

    transcripts = [f for f in down if isinstance(f, TranscriptionFrame)]
    assert [t.text for t in transcripts] == ["noise"]


async def test_low_confidence_partial_is_dropped(patch_ws):
    """A low-confidence partial_transcript is still filtered.

    The community-integration guide asks for >50% confidence filtering; applying
    it to interim frames keeps junk out of the visible transcript at no cost to
    the turn lifecycle.
    """
    patch_ws(
        "pipecat_slng.stt",
        [
            json.dumps({"type": "ready"}),
            json.dumps(
                {"type": "partial_transcript", "transcript": "noise", "confidence": 0.3}
            ),
        ],
    )
    stt = _make_stt()

    down, _ = await run_test(
        stt,
        frames_to_send=[
            _audio(),
            SleepFrame(sleep=0.3),
        ],
    )

    assert not [f for f in down if isinstance(f, InterimTranscriptionFrame)]


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
    """Without provider_key the BYOK header is never sent (route 1: default slng/ model)."""
    fake = patch_ws("pipecat_slng.stt", [json.dumps({"type": "ready"})])
    stt = _make_stt()

    await run_test(stt, frames_to_send=[SleepFrame(sleep=0.1)])

    assert "X-Slng-Provider-Key" not in fake.connect_headers


async def test_route3_external_model_no_key_no_byok_header(patch_ws):
    """Route 3: an external model WITHOUT provider_key sends only Authorization,
    no BYOK header. SLNG serves the external route via its own provider account
    (V21). The client never gates the route on the key (V17)."""
    fake = patch_ws("pipecat_slng.stt", [json.dumps({"type": "ready"})])
    stt = SlngSTTService(
        api_key="test-key",
        model="deepgram/nova:3",  # external route — no slng/ prefix
        sample_rate=16000,
    )

    await run_test(stt, frames_to_send=[SleepFrame(sleep=0.1)])

    assert fake.connect_headers["Authorization"] == "Bearer test-key"
    assert "X-Slng-Provider-Key" not in fake.connect_headers
    assert "deepgram/nova:3" in fake.connect_url


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
            _audio(),
            VADUserStoppedSpeakingFrame(),
            SleepFrame(sleep=0.2),
        ],
    )

    text_sends = [json.loads(s) for s in fake.sent if isinstance(s, str)]
    assert any(m.get("type") == "finalize" for m in text_sends)


async def test_vad_stop_then_final_marks_frame_finalized(patch_ws):
    """VAD stop + final_transcript marks the TranscriptionFrame finalized.

    This is the whole 1.0s: Pipecat 1.7.0 ends the user turn on a finalized
    transcript and otherwise waits out a safety-net timer anchored to
    speech_end + ttfs_p99_latency (turn_analyzer_user_turn_stop_strategy.py:236,
    :354). The SLNG bridge has no finalize-correlation field, so any final is
    the answer to an outstanding finalize.
    """
    fake = patch_ws("pipecat_slng.stt", [json.dumps({"type": "ready"})])
    stt = _make_stt()

    # The final must arrive AFTER the VAD-stop frame is processed, otherwise this
    # is the no-finalize-outstanding case instead. Pre-queuing it would race the
    # receive loop, so feed it on a delay.
    async def _feed_final_after_vad_stop():
        await asyncio.sleep(0.15)
        await fake.feed(json.dumps({"type": "final_transcript", "transcript": "hello"}))

    feeder = asyncio.create_task(_feed_final_after_vad_stop())
    down, _ = await run_test(
        stt,
        frames_to_send=[
            _audio(),
            VADUserStoppedSpeakingFrame(),
            SleepFrame(sleep=0.4),
        ],
    )
    await feeder

    transcripts = [f for f in down if isinstance(f, TranscriptionFrame)]
    assert transcripts, "no TranscriptionFrame pushed"
    assert transcripts[0].finalized is True


async def test_final_without_vad_stop_is_not_finalized(patch_ws):
    """A final arriving with no finalize outstanding stays unfinalized.

    confirm_finalize() no-ops unless request_finalize() ran (stt_service.py
    :221-223), so a mid-utterance final must not end the turn early. Guards
    against "simplifying" the fix into unconditionally setting finalized.
    """
    patch_ws(
        "pipecat_slng.stt",
        [
            json.dumps({"type": "ready"}),
            json.dumps({"type": "final_transcript", "transcript": "hello"}),
        ],
    )
    stt = _make_stt()

    down, _ = await run_test(
        stt,
        frames_to_send=[
            _audio(),
            SleepFrame(sleep=0.2),
        ],
    )

    transcripts = [f for f in down if isinstance(f, TranscriptionFrame)]
    assert transcripts, "no TranscriptionFrame pushed"
    assert transcripts[0].finalized is False


async def test_disconnect_sends_close(patch_ws):
    """On EndFrame the service sends {type: close} before tearing the socket down."""
    fake = patch_ws("pipecat_slng.stt", [json.dumps({"type": "ready"})])
    stt = _make_stt()

    await run_test(stt, frames_to_send=[SleepFrame(sleep=0.1)])

    text_sends = [json.loads(s) for s in fake.sent if isinstance(s, str)]
    assert any(m.get("type") == "close" for m in text_sends)


async def test_interruption_clears_pending_finalize(patch_ws):
    """An interruption must not leave a finalize outstanding.

    The base class clears the handshake only on VAD start
    (stt_service.py:594-595), but InterruptionFrame is emitted from an
    InterruptionWorkerFrame with no VAD coupling. Without this, an unanswered
    finalize outlives its turn and marks an unrelated later final as finalized,
    ending that turn early.
    """
    patch_ws("pipecat_slng.stt", [json.dumps({"type": "ready"})])
    stt = _make_stt()

    await run_test(
        stt,
        frames_to_send=[
            _audio(),
            VADUserStoppedSpeakingFrame(),
            SleepFrame(sleep=0.1),
            InterruptionFrame(),
            SleepFrame(sleep=0.1),
        ],
    )

    assert stt._finalize_requested is False
    assert stt._finalize_pending is False
