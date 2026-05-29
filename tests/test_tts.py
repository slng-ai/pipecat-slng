#
# Copyright (c) 2026, slng.ai
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Unit tests for SLNG TTS services (WebSocket + HTTP)."""

import asyncio
import json

from pipecat.frames.frames import ErrorFrame, TTSAudioRawFrame, TTSSpeakFrame
from pipecat.tests.utils import SleepFrame, run_test

from pipecat_slng import SlngHttpTTSService, SlngTTSService


def _make_tts():
    return SlngTTSService(
        api_key="test-key",
        voice="aura-2-thalia-en",
        sample_rate=24000,
    )


async def test_init_message_includes_voice(patch_ws):
    """Init message carries voice at top level and config fields."""
    fake = patch_ws("pipecat_slng.tts", [json.dumps({"type": "ready"})])
    tts = _make_tts()

    await run_test(tts, frames_to_send=[SleepFrame(sleep=0.1)])

    text_sends = [json.loads(s) for s in fake.sent if isinstance(s, str)]
    init = next(m for m in text_sends if m.get("type") == "init")
    assert init["voice"] == "aura-2-thalia-en"
    assert init["config"]["sample_rate"] == 24000


async def test_text_frame_sends_text_message(patch_ws):
    """A speak frame results in a text message to the server."""
    fake = patch_ws("pipecat_slng.tts", [json.dumps({"type": "ready"})])
    tts = _make_tts()

    await run_test(
        tts,
        frames_to_send=[TTSSpeakFrame(text="hi there"), SleepFrame(sleep=0.2)],
    )

    text_sends = [json.loads(s) for s in fake.sent if isinstance(s, str)]
    speak = next(m for m in text_sends if m.get("type") == "text")
    assert speak["text"] == "hi there"


async def test_binary_audio_becomes_audio_frame(patch_ws):
    """Server binary frames are emitted as TTSAudioRawFrame downstream."""
    fake = patch_ws(
        "pipecat_slng.tts",
        [json.dumps({"type": "ready"})],
    )
    tts = _make_tts()

    async def feed_audio_frame():
        # Deliver the binary audio only after run_tts has had a chance to
        # establish (and activate) the audio context for the utterance;
        # otherwise the receive loop drops bytes with no active context.
        await asyncio.sleep(0.2)
        await fake.feed(b"\x10\x11" * 100)

    feeder = asyncio.create_task(feed_audio_frame())
    try:
        down, _ = await run_test(
            tts,
            frames_to_send=[TTSSpeakFrame(text="hi"), SleepFrame(sleep=0.5)],
        )
    finally:
        await feeder

    audio_frames = [f for f in down if isinstance(f, TTSAudioRawFrame)]
    assert audio_frames and audio_frames[0].audio == b"\x10\x11" * 100


# ---------------------------------------------------------------------------
# HTTP TTS service
# ---------------------------------------------------------------------------


class FakeResponse:
    """Minimal stand-in for an aiohttp response."""

    def __init__(self, status=200, body=b"", text=""):
        self.status = status
        self._body = body
        self._text = text

    async def read(self):
        return self._body

    async def text(self):
        return self._text


class FakeRequestCtx:
    """Async-context-manager returned by ``FakeAiohttpSession.post``."""

    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *args):
        return False


class FakeAiohttpSession:
    """Records POST calls and returns a canned response."""

    def __init__(self, response):
        self._response = response
        self.calls: list = []

    def post(self, url, json=None, headers=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        return FakeRequestCtx(self._response)

    async def close(self):
        pass


def _make_http_tts(session):
    return SlngHttpTTSService(
        api_key="test-key",
        voice="aura-2-thalia-en",
        sample_rate=24000,
        aiohttp_session=session,
    )


async def test_http_posts_request_and_emits_audio():
    """HTTP TTS POSTs the right request and emits the returned audio."""
    session = FakeAiohttpSession(FakeResponse(status=200, body=b"\x10\x11" * 100))
    tts = _make_http_tts(session)

    down, _ = await run_test(
        tts,
        frames_to_send=[TTSSpeakFrame(text="hi there"), SleepFrame(sleep=0.2)],
    )

    assert session.calls, "no HTTP request was issued"
    call = session.calls[0]
    assert "/v1/bridges/unmute/tts/" in call["url"]
    assert call["headers"]["Authorization"] == "Bearer test-key"
    assert call["json"]["text"] == "hi there"
    assert call["json"]["voice"] == "aura-2-thalia-en"
    assert call["json"]["config"]["encoding"] == "linear16"
    assert call["json"]["config"]["sample_rate"] == 24000

    audio_frames = [f for f in down if isinstance(f, TTSAudioRawFrame)]
    assert audio_frames and audio_frames[0].audio == b"\x10\x11" * 100


async def test_http_non_200_yields_error_frame():
    """A non-200 HTTP response yields an ErrorFrame and no audio."""
    session = FakeAiohttpSession(FakeResponse(status=500, body=b"", text="boom"))
    tts = _make_http_tts(session)

    down, up = await run_test(
        tts,
        frames_to_send=[TTSSpeakFrame(text="hi"), SleepFrame(sleep=0.2)],
    )

    # ErrorFrames are pushed upstream by the pipecat TTSService base class.
    errors = [f for f in up if isinstance(f, ErrorFrame)]
    assert errors and "500" in errors[0].error
    assert not [f for f in down if isinstance(f, TTSAudioRawFrame)]
