#
# Copyright (c) 2026, slng.ai
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Unit tests for SLNG TTS services (WebSocket + HTTP)."""

import asyncio
import io
import json
import wave

import pytest
from pipecat.frames.frames import ErrorFrame, TTSAudioRawFrame, TTSSpeakFrame
from pipecat.tests.utils import SleepFrame, run_test

from pipecat_slng import SlngHttpTTSService, SlngTTSService, SlngTTSSettings


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

    def __init__(self, status=200, body=b"", text="", content_type="audio/pcm"):
        self.status = status
        self._body = body
        self._text = text
        self.headers = {"Content-Type": content_type}

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

    def post(self, url, json=None, headers=None, params=None):
        self.calls.append(
            {"url": url, "json": json, "headers": headers, "params": params}
        )
        return FakeRequestCtx(self._response)

    async def close(self):
        pass


def _make_http_tts(session, **overrides):
    return SlngHttpTTSService(
        api_key="test-key",
        voice="aura-2-thalia-en",
        sample_rate=24000,
        aiohttp_session=session,
        **overrides,
    )


def _make_wav(pcm: bytes, rate: int = 24000) -> bytes:
    """Wrap raw 16-bit mono PCM in a WAV (RIFF) container."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(pcm)
    return buf.getvalue()


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
    # The HTTP bridge body is {text, voice} only — no `config` object (sending
    # one makes the bridge reject the payload with a 400).
    assert "config" not in call["json"]
    assert call["params"] is None  # no region/world overrides set

    # A non-container response is passed through as raw PCM unchanged.
    audio_frames = [f for f in down if isinstance(f, TTSAudioRawFrame)]
    assert audio_frames and audio_frames[0].audio == b"\x10\x11" * 100


async def test_http_wav_response_is_decoded():
    """A WAV (RIFF) response is decoded to raw PCM at the file's sample rate."""
    pcm = b"\x10\x11" * 100
    session = FakeAiohttpSession(
        FakeResponse(
            status=200, body=_make_wav(pcm, rate=24000), content_type="audio/wav"
        )
    )
    tts = _make_http_tts(session)

    down, _ = await run_test(
        tts,
        frames_to_send=[TTSSpeakFrame(text="hi"), SleepFrame(sleep=0.2)],
    )

    audio_frames = [f for f in down if isinstance(f, TTSAudioRawFrame)]
    assert audio_frames
    assert audio_frames[0].audio == pcm  # RIFF/WAVE header stripped
    assert audio_frames[0].sample_rate == 24000


async def test_http_region_world_sent_as_query_params():
    """region/world-part overrides go in the query string, not headers."""
    session = FakeAiohttpSession(FakeResponse(status=200, body=b"\x00\x00" * 50))
    tts = _make_http_tts(
        session, region_override="eu-north-1", world_part_override="eu"
    )

    await run_test(
        tts,
        frames_to_send=[TTSSpeakFrame(text="hi"), SleepFrame(sleep=0.2)],
    )

    call = session.calls[0]
    assert call["params"] == {"region": "eu-north-1", "world-part": "eu"}
    assert "X-Region-Override" not in call["headers"]
    assert "X-World-Part-Override" not in call["headers"]


async def test_http_compressed_format_yields_error():
    """A compressed (e.g. MP3) response is rejected, not emitted as PCM."""
    session = FakeAiohttpSession(
        FakeResponse(
            status=200, body=b"ID3\x04\x00\x00\x00\x00", content_type="audio/mpeg"
        )
    )
    tts = _make_http_tts(session)

    down, up = await run_test(
        tts,
        frames_to_send=[TTSSpeakFrame(text="hi"), SleepFrame(sleep=0.2)],
    )

    errors = [f for f in up if isinstance(f, ErrorFrame)]
    assert errors and "format" in errors[0].error.lower()
    assert not [f for f in down if isinstance(f, TTSAudioRawFrame)]


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


async def test_ws_update_settings_reconnects(monkeypatch):
    """A changed setting triggers a reconnect so init is re-sent."""
    tts = _make_tts()

    calls: list = []

    async def fake_disconnect():
        calls.append("disconnect")

    async def fake_connect():
        calls.append("connect")

    monkeypatch.setattr(tts, "_disconnect", fake_disconnect)
    monkeypatch.setattr(tts, "_connect", fake_connect)

    changed = await tts._update_settings(SlngTTSSettings(voice="aura-2-asteria-en"))

    assert "voice" in changed
    assert calls == ["disconnect", "connect"]


async def test_ws_update_settings_noop_does_not_reconnect(monkeypatch):
    """An unchanged setting does not trigger a reconnect."""
    tts = _make_tts()

    calls: list = []

    async def fake_disconnect():
        calls.append("disconnect")

    async def fake_connect():
        calls.append("connect")

    monkeypatch.setattr(tts, "_disconnect", fake_disconnect)
    monkeypatch.setattr(tts, "_connect", fake_connect)

    # Same voice as the current setting → no change → no reconnect.
    changed = await tts._update_settings(SlngTTSSettings(voice="aura-2-thalia-en"))

    assert not changed
    assert calls == []


async def test_ws_region_and_world_headers_sent(patch_ws):
    """region_override + world_part_override map to X-Region-Override / X-World-Part-Override."""
    fake = patch_ws("pipecat_slng.tts", [json.dumps({"type": "ready"})])
    tts = SlngTTSService(
        api_key="test-key",
        voice="aura-2-thalia-en",
        sample_rate=24000,
        region_override="ap-southeast-2",
        world_part_override="ap",
    )

    await run_test(tts, frames_to_send=[SleepFrame(sleep=0.1)])

    assert fake.connect_headers["X-Region-Override"] == "ap-southeast-2"
    assert fake.connect_headers["X-World-Part-Override"] == "ap"


async def test_ws_provider_key_header_sent(patch_ws):
    """provider_key maps to the X-Slng-Provider-Key header (BYOK)."""
    fake = patch_ws("pipecat_slng.tts", [json.dumps({"type": "ready"})])
    tts = SlngTTSService(
        api_key="test-key",
        voice="aura-2-thalia-en",
        sample_rate=24000,
        provider_key="my-provider-key",
    )

    await run_test(tts, frames_to_send=[SleepFrame(sleep=0.1)])

    assert fake.connect_headers["X-Slng-Provider-Key"] == "my-provider-key"


async def test_ws_provider_key_header_absent_by_default(patch_ws):
    """Without provider_key the BYOK header is never sent (route 1: default slng/ model)."""
    fake = patch_ws("pipecat_slng.tts", [json.dumps({"type": "ready"})])
    tts = _make_tts()

    await run_test(tts, frames_to_send=[SleepFrame(sleep=0.1)])

    assert "X-Slng-Provider-Key" not in fake.connect_headers


async def test_ws_route3_external_model_no_key_no_byok_header(patch_ws):
    """Route 3 (WS TTS): an external model WITHOUT provider_key sends only
    Authorization, no BYOK header — served via SLNG's own provider account (V21)."""
    fake = patch_ws("pipecat_slng.tts", [json.dumps({"type": "ready"})])
    tts = SlngTTSService(
        api_key="test-key",
        model="deepgram/aura:2",  # external route — no slng/ prefix
        voice="aura-2-thalia-en",
        sample_rate=24000,
    )

    await run_test(tts, frames_to_send=[SleepFrame(sleep=0.1)])

    assert fake.connect_headers["Authorization"] == "Bearer test-key"
    assert "X-Slng-Provider-Key" not in fake.connect_headers
    assert "deepgram/aura:2" in fake.connect_url


async def test_http_provider_key_header_sent():
    """provider_key maps to the X-Slng-Provider-Key request header (BYOK)."""
    session = FakeAiohttpSession(FakeResponse(status=200, body=b"\x00\x00" * 50))
    tts = _make_http_tts(session, provider_key="my-provider-key")

    await run_test(
        tts,
        frames_to_send=[TTSSpeakFrame(text="hi"), SleepFrame(sleep=0.2)],
    )

    call = session.calls[0]
    assert call["headers"]["X-Slng-Provider-Key"] == "my-provider-key"


async def test_http_provider_key_header_absent_by_default():
    """Without provider_key the BYOK header is never sent (route 1: default slng/ model)."""
    session = FakeAiohttpSession(FakeResponse(status=200, body=b"\x00\x00" * 50))
    tts = _make_http_tts(session)

    await run_test(
        tts,
        frames_to_send=[TTSSpeakFrame(text="hi"), SleepFrame(sleep=0.2)],
    )

    call = session.calls[0]
    assert "X-Slng-Provider-Key" not in call["headers"]


async def test_http_route3_external_model_no_key_no_byok_header():
    """Route 3 (HTTP TTS): an external model WITHOUT provider_key sends only
    Authorization, no BYOK header — served via SLNG's own provider account (V21)."""
    session = FakeAiohttpSession(FakeResponse(status=200, body=b"\x00\x00" * 50))
    tts = _make_http_tts(session, model="deepgram/aura:2")

    await run_test(
        tts,
        frames_to_send=[TTSSpeakFrame(text="hi"), SleepFrame(sleep=0.2)],
    )

    call = session.calls[0]
    assert call["headers"]["Authorization"] == "Bearer test-key"
    assert "X-Slng-Provider-Key" not in call["headers"]
    assert "deepgram/aura:2" in call["url"]


async def test_v19_connect_rejection_includes_server_body(monkeypatch):
    """A rejected WS upgrade surfaces the server response body, not just the status."""
    from websockets.datastructures import Headers
    from websockets.exceptions import InvalidStatus
    from websockets.http11 import Response

    body = b'{"error":"BYOK is only supported for external STT/TTS routes"}'
    rejection = InvalidStatus(Response(400, "Bad Request", Headers(), body))

    async def _reject(url, **kwargs):
        raise rejection

    monkeypatch.setattr("pipecat_slng.tts.websocket_connect", _reject)
    tts = _make_tts()

    pushed: list[str] = []

    async def _record_error(error_msg: str, exception: BaseException | None = None):
        pushed.append(error_msg)

    monkeypatch.setattr(tts, "push_error", _record_error)

    with pytest.raises(InvalidStatus):
        await tts._connect_websocket()

    assert pushed and "BYOK is only supported" in pushed[0]
    assert "HTTP 400" in pushed[0]


async def test_ws_disconnect_sends_close(patch_ws):
    """On EndFrame the WS-TTS service sends {type: close} before teardown."""
    fake = patch_ws("pipecat_slng.tts", [json.dumps({"type": "ready"})])
    tts = _make_tts()

    await run_test(tts, frames_to_send=[SleepFrame(sleep=0.1)])

    text_sends = [json.loads(s) for s in fake.sent if isinstance(s, str)]
    assert any(m.get("type") == "close" for m in text_sends)


async def test_flush_audio_sends_flush(patch_ws):
    """flush_audio() sends {type: flush} to the bridge."""
    fake = patch_ws("pipecat_slng.tts", [])
    tts = _make_tts()
    tts._websocket = fake

    await tts.flush_audio("ctx-1")

    text_sends = [json.loads(s) for s in fake.sent if isinstance(s, str)]
    assert any(m.get("type") == "flush" for m in text_sends)


async def test_interrupt_sends_clear(patch_ws, monkeypatch):
    """on_audio_context_interrupted sends {type: clear} to the bridge."""
    fake = patch_ws("pipecat_slng.tts", [])
    tts = _make_tts()
    tts._websocket = fake

    # Stub out base-class machinery that needs full pipeline state.
    async def _noop(*args, **kwargs):
        pass

    monkeypatch.setattr(tts, "stop_all_metrics", _noop)
    # super().on_audio_context_interrupted touches AIService context bookkeeping;
    # patch it on the parent class so the chain no-ops cleanly.
    from pipecat.services.tts_service import WebsocketTTSService

    monkeypatch.setattr(WebsocketTTSService, "on_audio_context_interrupted", _noop)

    await tts.on_audio_context_interrupted("ctx-1")

    text_sends = [json.loads(s) for s in fake.sent if isinstance(s, str)]
    assert any(m.get("type") == "clear" for m in text_sends)


# ---------------------------------------------------------------------------
# V15: server close after audio_end/flushed is expected lifecycle
# ---------------------------------------------------------------------------


async def test_v15_expected_close_reconnects_quietly_three_times(monkeypatch):
    """Three rapid per-utterance server closes reconnect without error.

    Each connection lives well under pipecat's 5s stability threshold; without
    the expected-close handling the third close would trip the consecutive
    quick-failure cap and shut the receive loop down with an ErrorFrame.
    """
    from conftest import FakeWebSocket

    fakes: list[FakeWebSocket] = []

    async def _connect(url, **kwargs):
        fake = FakeWebSocket([json.dumps({"type": "ready"})])
        fakes.append(fake)
        return fake

    monkeypatch.setattr("pipecat_slng.tts.websocket_connect", _connect)
    tts = _make_tts()

    async def drive_three_closes():
        for i in range(3):
            while len(fakes) < i + 1:
                await asyncio.sleep(0.01)
            await fakes[i].feed(json.dumps({"type": "audio_end"}))
            await fakes[i].close()
            while len(fakes) < i + 2:
                await asyncio.sleep(0.01)

    driver = asyncio.create_task(drive_three_closes())
    try:
        down, up = await run_test(tts, frames_to_send=[SleepFrame(sleep=1.0)])
    finally:
        await asyncio.wait_for(driver, timeout=5)

    # Initial connection + one quiet reconnect per expected close.
    assert len(fakes) >= 4
    for fake in fakes:
        text_sends = [json.loads(s) for s in fake.sent if isinstance(s, str)]
        assert any(m.get("type") == "init" for m in text_sends)
    assert not [f for f in down if isinstance(f, ErrorFrame)]
    assert not [f for f in up if isinstance(f, ErrorFrame)]


async def test_v15_audio_end_and_flushed_set_expected_close(monkeypatch):
    """Both completion messages arm the expected-close flag."""
    tts = _make_tts()
    monkeypatch.setattr(tts, "get_active_audio_context_id", lambda: None)

    assert tts._expect_server_close is False
    await tts._process_message({"type": "audio_end"})
    assert tts._expect_server_close is True

    tts._expect_server_close = False
    await tts._process_message({"type": "flushed"})
    assert tts._expect_server_close is True


async def test_v15_run_tts_resets_stale_expected_close(patch_ws, monkeypatch):
    """A new utterance clears a stale flag so it cannot mask a real failure.

    Covers servers that do NOT close after audio_end (e.g. aura): the flag
    armed by the previous utterance must not survive into the next one.
    """
    fake = patch_ws("pipecat_slng.tts", [])
    tts = _make_tts()
    tts._websocket = fake
    tts._ready_event.set()

    async def _noop(*args, **kwargs):
        pass

    monkeypatch.setattr(tts, "start_tts_usage_metrics", _noop)

    tts._expect_server_close = True
    async for _ in tts.run_tts("hi", "ctx-1"):
        pass

    assert tts._expect_server_close is False
    text_sends = [json.loads(s) for s in fake.sent if isinstance(s, str)]
    assert any(m.get("type") == "text" for m in text_sends)


async def test_v15_unexpected_close_delegates_to_base(monkeypatch):
    """With the flag unset, closes keep the full base-class failure handling."""
    from pipecat.services.websocket_service import WebsocketService

    tts = _make_tts()
    calls: list = []

    async def fake_base(self, error_message, report_error, error=None):
        calls.append(error_message)
        return False

    monkeypatch.setattr(WebsocketService, "_maybe_try_reconnect", fake_base)

    async def _report(frame):
        pass

    assert tts._expect_server_close is False
    result = await tts._maybe_try_reconnect("boom", _report)

    assert calls == ["boom"]
    assert result is False
