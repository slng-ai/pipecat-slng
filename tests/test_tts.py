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
from typing import Any

import pytest
from websockets.protocol import State
from pipecat.frames.frames import (
    ErrorFrame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    TextFrame,
    TTSAudioRawFrame,
    TTSSpeakFrame,
    TTSStoppedFrame,
)
from pipecat.tests.utils import SleepFrame, run_test

from pipecat_slng import SlngHttpTTSService, SlngTTSService, SlngTTSSettings


def _make_tts():
    return SlngTTSService(
        api_key="test-key",
        voice="aura-2-thalia-en",
        sample_rate=24000,
    )


async def test_init_message_includes_voice(patch_ws):
    """Init carries voice/config fields and omits unset pronunciation."""
    fake = patch_ws("pipecat_slng.tts", [json.dumps({"type": "ready"})])
    tts = _make_tts()

    await run_test(tts, frames_to_send=[SleepFrame(sleep=0.1)])

    text_sends = [json.loads(s) for s in fake.sent if isinstance(s, str)]
    init = next(m for m in text_sends if m.get("type") == "init")
    assert init["voice"] == "aura-2-thalia-en"
    assert init["config"]["sample_rate"] == 24000
    assert "pronunciation" not in init["config"]


@pytest.mark.parametrize(
    ("pronunciation", "via_settings"),
    [
        ({"mode": "rewrite", "name": "brand-pronunciations"}, False),
        ({"mode": "rewrite", "dictionary_id": "pd_01abc"}, True),
    ],
)
async def test_ws_pronunciation_ref_passed_through(
    patch_ws: Any, pronunciation: dict[str, str], via_settings: bool
) -> None:
    """Name and ID references reach init config unchanged."""
    fake = patch_ws("pipecat_slng.tts", [json.dumps({"type": "ready"})])
    settings = SlngTTSSettings(pronunciation=pronunciation) if via_settings else None
    tts = SlngTTSService(
        api_key="test-key",
        voice="aura-2-thalia-en",
        sample_rate=24000,
        pronunciation=None if via_settings else pronunciation,
        settings=settings,
    )

    await run_test(tts, frames_to_send=[SleepFrame(sleep=0.1)])

    text_sends = [json.loads(s) for s in fake.sent if isinstance(s, str)]
    init = next(m for m in text_sends if m.get("type") == "init")
    assert init["config"]["pronunciation"] == pronunciation


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
    """A changed setting reconnects without clearing unrelated settings."""
    pronunciation = {"mode": "rewrite", "name": "brand-pronunciations"}
    tts = SlngTTSService(
        api_key="test-key",
        voice="aura-2-thalia-en",
        sample_rate=24000,
        pronunciation=pronunciation,
    )

    calls: list = []

    async def fake_disconnect():
        calls.append("disconnect")

    async def fake_connect():
        calls.append("connect")

    monkeypatch.setattr(tts, "_disconnect", fake_disconnect)
    monkeypatch.setattr(tts, "_connect", fake_connect)

    changed = await tts._update_settings(SlngTTSSettings(voice="aura-2-asteria-en"))

    assert "voice" in changed
    assert "pronunciation" not in changed
    assert tts._settings.pronunciation == pronunciation
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


async def test_idle_keepalive_sent(patch_ws, monkeypatch):
    """An idle WS-TTS session sends {"type": "keepalive"}.

    The TTS bridge documents keepalive as preventing an inactivity close. The
    STT side of this plugin already sends one; without this the socket can be
    dropped mid-call, putting a reconnect plus init on the path to the next
    segment's first audio.
    """
    monkeypatch.setattr("pipecat_slng.tts._KEEPALIVE_INTERVAL", 0.05)
    fake = patch_ws("pipecat_slng.tts", [json.dumps({"type": "ready"})])
    tts = _make_tts()

    await run_test(tts, frames_to_send=[SleepFrame(sleep=0.3)])

    text_sends = [json.loads(s) for s in fake.sent if isinstance(s, str)]
    assert any(m.get("type") == "keepalive" for m in text_sends)


async def test_keepalive_stops_on_disconnect(patch_ws, monkeypatch):
    """The keepalive task does not outlive its socket."""
    monkeypatch.setattr("pipecat_slng.tts._KEEPALIVE_INTERVAL", 0.05)
    fake = patch_ws("pipecat_slng.tts", [json.dumps({"type": "ready"})])
    tts = _make_tts()

    await run_test(tts, frames_to_send=[SleepFrame(sleep=0.2)])

    assert tts._keepalive_task is None
    # Reopen the fake so a surviving task COULD send: with the socket left
    # CLOSED the handler's open-state guard suppresses sends anyway, and the
    # assertion below would pass whether or not the task was cancelled.
    fake.state = State.OPEN
    sent_after_stop = len(fake.sent)
    await asyncio.sleep(0.2)
    assert len(fake.sent) == sent_after_stop


async def test_expected_close_reports_arming_event(patch_ws):
    """The expected-close flag records which event armed it.

    `flushed` is sent after every utterance on every route; only some upstreams
    follow `audio_end` with an actual close. Recording which one armed the flag
    makes "does any route close after flushed?" answerable from real logs,
    instead of narrowing the arming on speculation and risking a regression on
    the upstream the flag was added for.
    """
    patch_ws("pipecat_slng.tts", [json.dumps({"type": "ready"})])
    tts = _make_tts()

    await tts._process_message({"type": "flushed"})
    assert tts._expect_server_close is True
    assert tts._expect_server_close_reason == "flushed"

    tts._expect_server_close = False
    tts._expect_server_close_reason = None

    await tts._process_message({"type": "audio_end"})
    assert tts._expect_server_close is True
    assert tts._expect_server_close_reason == "audio_end"


async def test_keepalive_survives_expected_close_reconnect(monkeypatch):
    """Keepalive keeps firing after a per-utterance close swaps the socket.

    `_maybe_try_reconnect` swaps sockets via `_disconnect_websocket`/
    `_connect_websocket`, which never touch `_keepalive_task`. A handler that
    broke out of its loop on a send error would leave a *completed* task behind
    — truthy, so `_connect`'s `not self._keepalive_task` guard would never
    recreate it — and the keepalive would be silently dead for the rest of the
    session, which is the idle close it exists to prevent.
    """
    from conftest import FakeWebSocket

    monkeypatch.setattr("pipecat_slng.tts._KEEPALIVE_INTERVAL", 0.05)
    fakes: list[FakeWebSocket] = []

    async def _connect(url, **kwargs):
        fake = FakeWebSocket([json.dumps({"type": "ready"})])
        fakes.append(fake)
        return fake

    monkeypatch.setattr("pipecat_slng.tts.websocket_connect", _connect)
    tts = _make_tts()

    async def close_first_socket():
        while not fakes:
            await asyncio.sleep(0.01)

        # Make the next keepalive send fail, then close: exactly the transient
        # error + reconnect sequence that killed the task before.
        original_send = fakes[0].send

        async def _boom(_data):
            raise ConnectionError("keepalive send failed")

        monkeypatch.setattr(fakes[0], "send", _boom)
        await asyncio.sleep(0.12)
        monkeypatch.setattr(fakes[0], "send", original_send)
        await fakes[0].feed(json.dumps({"type": "audio_end"}))
        await fakes[0].close()

    driver = asyncio.create_task(close_first_socket())
    try:
        await run_test(tts, frames_to_send=[SleepFrame(sleep=0.6)])
    finally:
        await asyncio.wait_for(driver, timeout=5)

    assert len(fakes) >= 2, "expected close should have opened a replacement socket"
    replacement_keepalives = [
        s
        for s in fakes[-1].sent
        if isinstance(s, str) and json.loads(s).get("type") == "keepalive"
    ]
    assert replacement_keepalives, (
        "keepalive died: no keepalive on the replacement socket after a send "
        "error + per-utterance reconnect"
    )


# ---------------------------------------------------------------------------
# Terminal completion: one stop per utterance, on either terminal form
# ---------------------------------------------------------------------------
#
# Every scenario below asserts what a consumer sees — audio, then exactly one
# matching TTSStoppedFrame — and finishes well inside the 3s context inactivity
# fallback. With push_stop_frames=False that fallback cannot emit a stop at all,
# so any stop observed here is protocol-driven.


def _sent_types(fake) -> list[str]:
    return [json.loads(s).get("type") for s in fake.sent if isinstance(s, str)]


async def _await_sent(fake, kind: str, count: int = 1, timeout: float = 3.0):
    """Block until ``count`` client→server messages of ``kind`` are on the wire."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if _sent_types(fake).count(kind) >= count:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"timed out waiting for {count} {kind!r} message(s)")


async def _await_sockets(fakes: list, count: int, timeout: float = 3.0):
    """Block until ``count`` connections have been opened."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while len(fakes) < count and loop.time() < deadline:
        await asyncio.sleep(0.01)
    assert len(fakes) >= count, f"expected {count} connections, saw {len(fakes)}"


def _stops(frames) -> list[TTSStoppedFrame]:
    return [f for f in frames if isinstance(f, TTSStoppedFrame)]


def _audio(frames) -> list[TTSAudioRawFrame]:
    return [f for f in frames if isinstance(f, TTSAudioRawFrame)]


def _collect_sockets(monkeypatch) -> list:
    """Patch the TTS connect to record every socket it hands out."""
    from conftest import FakeWebSocket

    fakes: list[FakeWebSocket] = []

    async def _connect(url, **kwargs):
        fake = FakeWebSocket([json.dumps({"type": "ready"})])
        fakes.append(fake)
        return fake

    monkeypatch.setattr("pipecat_slng.tts.websocket_connect", _connect)
    return fakes


@pytest.mark.parametrize(
    "terminals",
    [
        ["audio_end"],
        ["flushed"],
        ["audio_end", "audio_end"],
        ["flushed", "flushed"],
        ["audio_end", "flushed"],
        ["flushed", "audio_end"],
    ],
    ids=["audio_end", "flushed", "audio_end-x2", "flushed-x2", "both", "both-reversed"],
)
async def test_tts_terminal_completion(patch_ws, terminals):
    """Either terminal form finishes the utterance exactly once, after its audio.

    The bridge can end a turn with `audio_end` alone and keep the connection
    open — Deepgram and Gradium routes both translate their native completion to
    it. Recognising only `flushed` left those utterances with no downstream
    completion at all. A repeated terminal, or the other form arriving too, must
    not add a second one.
    """
    fake = patch_ws("pipecat_slng.tts", [json.dumps({"type": "ready"})])
    tts = _make_tts()
    audio = b"\x21\x43" * 100

    async def serve():
        await _await_sent(fake, "flush")
        await fake.feed(audio)
        for terminal in terminals:
            await fake.feed(json.dumps({"type": terminal}))

    server = asyncio.create_task(serve())
    try:
        down, up = await run_test(
            tts, frames_to_send=[TTSSpeakFrame(text="hi"), SleepFrame(sleep=0.5)]
        )
    finally:
        await asyncio.wait_for(server, timeout=5)

    stops, audio_frames = _stops(down), _audio(down)
    assert [f.audio for f in audio_frames] == [audio]
    assert len(stops) == 1
    assert down.index(stops[0]) > down.index(audio_frames[-1])
    assert stops[0].context_id == audio_frames[0].context_id
    assert not [f for f in up if isinstance(f, ErrorFrame)]


async def test_tts_fragments_complete_once_at_turn_end(patch_ws):
    """A multi-fragment turn is one utterance: no early stop, no truncated audio.

    `segment_end` marks a provider segment, not the end of the turn, and the
    turn's own terminal only counts once its final flush has been sent.
    """
    fake = patch_ws("pipecat_slng.tts", [json.dumps({"type": "ready"})])
    tts = _make_tts()
    first, second = b"\x01\x02" * 50, b"\x03\x04" * 50

    async def serve():
        # Two sentences on the wire with the turn still open: the aggregator
        # holds the last one back until the response ends.
        await _await_sent(fake, "text", count=2)
        await fake.feed(first)
        await fake.feed(json.dumps({"type": "segment_end"}))
        # Before the turn's own flush, so it can only be ending a segment.
        await fake.feed(json.dumps({"type": "audio_end"}))
        await _await_sent(fake, "flush")
        await fake.feed(second)
        await fake.feed(json.dumps({"type": "audio_end"}))

    server = asyncio.create_task(serve())
    try:
        down, up = await run_test(
            tts,
            frames_to_send=[
                LLMFullResponseStartFrame(),
                TextFrame("Hello there. "),
                TextFrame("How are you? "),
                TextFrame("Fine thanks. "),
                SleepFrame(sleep=0.3),
                LLMFullResponseEndFrame(),
                SleepFrame(sleep=0.5),
            ],
        )
    finally:
        await asyncio.wait_for(server, timeout=5)

    stops, audio_frames = _stops(down), _audio(down)
    assert _sent_types(fake).count("text") == 3
    assert [f.audio for f in audio_frames] == [first, second]
    assert len({f.context_id for f in audio_frames}) == 1
    assert len(stops) == 1
    assert down.index(stops[0]) > down.index(audio_frames[-1])
    assert not [f for f in up if isinstance(f, ErrorFrame)]


async def test_tts_pipelined_utterances_keep_their_own_audio(patch_ws):
    """Two turns in flight each complete against their own synthesis context.

    Attribution used to follow Pipecat's playback cursor, which lags synthesis:
    with the first utterance still draining downstream, the second one's audio
    was appended behind the first context's end marker and never played.
    """
    fake = patch_ws("pipecat_slng.tts", [json.dumps({"type": "ready"})])
    tts = _make_tts()
    first, second = b"\x11\x11" * 100, b"\x22\x22" * 100

    async def serve():
        await _await_sent(fake, "flush", count=2)  # both turns submitted
        await fake.feed(first)
        await fake.feed(json.dumps({"type": "audio_end"}))
        await fake.feed(second)
        await fake.feed(json.dumps({"type": "audio_end"}))

    server = asyncio.create_task(serve())
    try:
        down, up = await run_test(
            tts,
            frames_to_send=[
                TTSSpeakFrame(text="one"),
                TTSSpeakFrame(text="two"),
                SleepFrame(sleep=0.8),
            ],
        )
    finally:
        await asyncio.wait_for(server, timeout=5)

    stops, audio_frames = _stops(down), _audio(down)
    assert [f.audio for f in audio_frames] == [first, second]
    ctx_a, ctx_b = audio_frames[0].context_id, audio_frames[1].context_id
    assert ctx_a != ctx_b
    assert [s.context_id for s in stops] == [ctx_a, ctx_b]
    assert not [f for f in up if isinstance(f, ErrorFrame)]


async def test_tts_completion_keeps_the_socket_open(monkeypatch):
    """Completing an utterance leaves the connection available for the next one."""
    fakes = _collect_sockets(monkeypatch)
    tts = _make_tts()

    async def serve():
        await _await_sockets(fakes, 1)
        for turn in (1, 2):
            await _await_sent(fakes[0], "flush", count=turn)
            await fakes[0].feed(bytes([turn]) * 100)
            await fakes[0].feed(json.dumps({"type": "audio_end"}))

    server = asyncio.create_task(serve())
    try:
        down, up = await run_test(
            tts,
            frames_to_send=[
                TTSSpeakFrame(text="one"),
                SleepFrame(sleep=0.3),
                TTSSpeakFrame(text="two"),
                SleepFrame(sleep=0.5),
            ],
        )
    finally:
        await asyncio.wait_for(server, timeout=5)

    assert len(fakes) == 1, "completion must not cost a reconnect"
    assert [f.audio for f in _audio(down)] == [b"\x01" * 100, b"\x02" * 100]
    assert len(_stops(down)) == 2
    assert not [f for f in up if isinstance(f, ErrorFrame)]


async def test_tts_interruption_retires_the_contaminated_stream(monkeypatch):
    """An abandoned turn's stream is dropped before a new turn uses it.

    `cleared` is not a portable drain barrier across gateway runtimes, so audio
    the provider had already queued for the abandoned turn can still arrive.
    Keeping that stream would credit it to the next utterance.
    """
    fakes = _collect_sockets(monkeypatch)
    tts = _make_tts()
    stale, fresh = b"\x99\x99" * 100, b"\x33\x33" * 100

    async def serve():
        await _await_sockets(fakes, 1)
        await _await_sent(fakes[0], "clear")
        await _await_sockets(fakes, 2)
        await _await_sent(fakes[1], "flush")
        await fakes[0].feed(stale)  # late output from the abandoned stream
        await fakes[1].feed(fresh)
        await fakes[1].feed(json.dumps({"type": "audio_end"}))

    server = asyncio.create_task(serve())
    try:
        down, _ = await run_test(
            tts,
            frames_to_send=[
                TTSSpeakFrame(text="interrupt me"),
                SleepFrame(sleep=0.2),
                InterruptionFrame(),
                SleepFrame(sleep=0.2),
                TTSSpeakFrame(text="after"),
                SleepFrame(sleep=0.6),
            ],
        )
    finally:
        await asyncio.wait_for(server, timeout=5)

    assert [f.audio for f in _audio(down)] == [fresh]
    assert len(_stops(down)) == 1


async def test_tts_zero_audio_terminal_still_completes_the_context(patch_ws):
    """A terminal with no audio closes the context so Pipecat can report it."""
    fake = patch_ws("pipecat_slng.tts", [json.dumps({"type": "ready"})])
    tts = _make_tts()

    async def serve():
        await _await_sent(fake, "flush")
        await fake.feed(json.dumps({"type": "audio_end"}))

    server = asyncio.create_task(serve())
    try:
        down, up = await run_test(
            tts, frames_to_send=[TTSSpeakFrame(text="hi"), SleepFrame(sleep=0.5)]
        )
    finally:
        await asyncio.wait_for(server, timeout=5)

    assert not _audio(down)
    assert len(_stops(down)) == 1
    errors = [f for f in up if isinstance(f, ErrorFrame)]
    assert errors and "no audio" in errors[0].error


async def test_tts_missing_terminal_fabricates_no_completion(patch_ws):
    """Without a terminal message there is no completion — only the fallback."""
    fake = patch_ws("pipecat_slng.tts", [json.dumps({"type": "ready"})])
    tts = _make_tts()
    audio = b"\x44\x44" * 100

    async def serve():
        await _await_sent(fake, "flush")
        await fake.feed(audio)

    server = asyncio.create_task(serve())
    try:
        down, _ = await run_test(
            tts, frames_to_send=[TTSSpeakFrame(text="hi"), SleepFrame(sleep=0.5)]
        )
    finally:
        await asyncio.wait_for(server, timeout=5)

    assert [f.audio for f in _audio(down)] == [audio]
    assert not _stops(down)


async def test_tts_empty_input_creates_no_utterance(patch_ws):
    """Text that never reaches synthesis produces neither audio nor a stop."""
    fake = patch_ws("pipecat_slng.tts", [json.dumps({"type": "ready"})])
    tts = _make_tts()

    down, _ = await run_test(
        tts, frames_to_send=[TTSSpeakFrame(text="   "), SleepFrame(sleep=0.3)]
    )

    assert "text" not in _sent_types(fake)
    assert not _audio(down)
    assert not _stops(down)


async def test_tts_rejects_per_fragment_contexts_before_connecting(monkeypatch):
    """Explicit per-fragment contexts are refused, not silently overridden.

    The bridge returns one unlabelled stream per input turn, so fragment-sized
    contexts have nothing to attribute that stream to.
    """
    fakes = _collect_sockets(monkeypatch)

    with pytest.raises(ValueError, match="reuse_context_id_within_turn"):
        SlngTTSService(
            api_key="test-key",
            voice="aura-2-thalia-en",
            sample_rate=24000,
            reuse_context_id_within_turn=False,
        )

    assert not fakes, "the configuration must be rejected before any connection"
