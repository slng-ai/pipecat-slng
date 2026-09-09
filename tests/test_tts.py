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
from pipecat.observers.base_observer import FramePushed
from pipecat.transcriptions.language import Language
from pipecat.processors.frame_processor import FrameDirection
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

    tts._reserve_wire_turn("ctx-1")
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


@pytest.mark.parametrize("enabled", [False, True])
async def test_tts_interruption_retires_the_contaminated_stream(monkeypatch, enabled):
    """An abandoned turn's stream is dropped before a new turn uses it.

    `cleared` is not a portable drain barrier across gateway runtimes, so audio
    the provider had already queued for the abandoned turn can still arrive.
    Keeping that stream would credit it to the next utterance.
    """
    fakes = _collect_sockets(monkeypatch)
    tts = SlngTTSService(
        api_key="test", sample_rate=24000, warm_standby_enabled=enabled
    )
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

    assert all(ws.state is State.CLOSED for ws in fakes)


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


@pytest.mark.parametrize("enabled", [False, True])
async def test_tts_rejects_per_fragment_contexts_before_connecting(
    monkeypatch, enabled
):
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
            warm_standby_enabled=enabled,
        )

    assert not fakes, "the configuration must be rejected before any connection"


# ---------------------------------------------------------------------------
# Warm-standby measurement harness: offline qualification of its derivation
# ---------------------------------------------------------------------------
#
# Pure bookkeeping over a synthetic event log, so it belongs offline rather than
# behind the live gate in ``test_live_smoke.py``, whose module mark would skip it
# in CI. The harness has to be trustworthy before its numbers mean anything: an
# earlier version derived a negative request-to-text span from a delayed
# observer, passed a batch in which nothing completed, and reported zero live
# connections with a socket still open.


class _FakeService:
    """Clock holder and frame-identity sentinel for a scripted batch."""

    def __init__(self):
        """Start the scripted clock at zero."""
        self.seconds = 0.0

    def get_clock(self):
        """Stand in for the pipeline clock the harness reads."""
        return self

    def get_time(self) -> int:
        """Elapsed nanoseconds, as ``SystemClock`` reports them."""
        return int(self.seconds * 1e9)


async def _replay(script, texts):
    """Replay a scripted batch through the real measurement helpers.

    Each entry is ``(at, kind, payload)``. ``wire`` and ``send`` entries advance
    the scripted clock, since those are recorded by code running on the wire's
    own timeline. Frame entries do not: their ``at`` is the pipeline timestamp
    the frame was stamped with, so listing one out of order models an observer
    that was notified late.
    """
    from test_live_smoke import _TurnObserver, _WireLog

    # Any: the harness only ever reads the clock off this and compares frame
    # source/destination against it by identity.
    service: Any = _FakeService()
    other: Any = _FakeService()  # the processor on the far side of each push
    log = _WireLog(service)
    observer = _TurnObserver(service, texts)
    turns: list[dict] = []

    for at, kind, payload in script:
        stamp = int(at * 1e9)
        if kind in ("wire", "spare_wire"):
            service.seconds = at
            name, nbytes = payload if isinstance(payload, tuple) else (payload, 0)
            log.mark(2 if kind == "spare_wire" else 1, name, nbytes, b"\x00" * nbytes)
            continue
        if kind == "send":
            service.seconds = at
            index, context_id, ok = payload
            turns.append(
                {
                    "index": index,
                    "context_id": context_id,
                    "attempt_at": at,
                    "sent_ok": ok,
                }
            )
            continue

        if kind == "complete":
            observer.completed_at[payload] = at
            continue

        frame: Any
        source, destination = service, other
        if kind == "request":
            frame, source, destination = TTSSpeakFrame(text=payload), other, service
        elif kind == "audio":
            context_id, nbytes = payload
            frame = TTSAudioRawFrame(
                audio=b"\x00" * nbytes,
                sample_rate=24000,
                num_channels=1,
                context_id=context_id,
            )
        elif kind == "stop":
            frame = TTSStoppedFrame(context_id=payload)
        else:
            frame = ErrorFrame(error=payload)
        await observer.on_push_frame(
            FramePushed(
                source=source,
                destination=destination,
                frame=frame,
                direction=FrameDirection.DOWNSTREAM,
                timestamp=stamp,
            )
        )

    from test_live_smoke import _spans

    return _spans(turns, observer, log, texts), observer, log


def _one_turn(
    *,
    sent_ok: bool = True,
    text_send: bool = True,
    audio: bool = True,
    terminal: str | None = "recv:audio_end",
    stop: bool = True,
    received: int = 400,
    emitted: int = 400,
) -> list:
    """A single complete turn, with any one part missing or corrupted."""
    script: list[tuple[float, str, Any]] = [
        (0.0, "wire", "connect_start"),
        (0.2, "wire", "open"),
        (0.2, "wire", "send:init"),
        (0.4, "wire", "recv:ready"),
        (0.3, "request", "one"),
        (0.45, "send", (0, "ctx-a", sent_ok)),
    ]
    if text_send:
        script.append((0.5, "wire", "send:text"))
    if audio:
        script.append((0.8, "wire", ("audio", received)))
    if terminal:
        script.append((1.0, "wire", terminal))
    if audio:
        script.append((1.05, "audio", ("ctx-a", emitted)))
    if stop:
        script.append((1.1, "stop", "ctx-a"))
        script.append((1.12, "complete", "ctx-a"))
    return script


async def test_measurement_qualification():
    """The harness must reject a batch it cannot actually account for.

    One scenario over the real helpers, covering the ways the previous version
    reported a plausible number for something it had not observed: a delayed
    observer notification, a send that never reached the wire, audio that
    belongs to another context, a turn with no terminal or no downstream
    completion, a reported service error, and a receive loop cancelled while its
    socket was still open.
    """
    texts = ["one", "two", "three"]

    # --- healthy pair with a missing middle send -----------------------------
    #
    # Utterance 0's request is stamped at 0.3 but replayed after the readiness
    # events, the way a busy observer is notified late; utterance 1 is requested
    # and never reaches run_tts; utterance 2 is complete. Utterance 0 must keep
    # its own spans, utterance 1 must stay absent rather than inherit them, and
    # utterance 2's audio must not be credited backwards.
    rows, observer, log = await _replay(
        [
            (0.0, "wire", "connect_start"),
            (0.2, "wire", "open"),
            (0.2, "wire", "send:init"),
            (0.4, "wire", "recv:ready"),
            (0.3, "request", "one"),
            (0.45, "send", (0, "ctx-a", True)),
            (0.5, "wire", "send:text"),
            (0.8, "wire", ("audio", 400)),
            (1.0, "wire", "recv:audio_end"),
            (1.05, "audio", ("ctx-a", 400)),
            (1.1, "stop", "ctx-a"),
            (1.12, "complete", "ctx-a"),
            (2.0, "request", "two"),
            (3.0, "request", "three"),
            (3.1, "send", (2, "ctx-c", True)),
            (3.2, "wire", "send:text"),
            (3.5, "wire", ("audio", 900)),
            (3.7, "wire", "recv:flushed"),
            (3.75, "audio", ("ctx-c", 900)),
            (3.8, "stop", "ctx-c"),
            (3.82, "complete", "ctx-c"),
        ],
        texts,
    )

    assert [r["valid"] for r in rows] == [True, False, True]
    # Timestamps, not delivery order: replaying the request last must not make
    # its request->text span negative or drag the setup events into it.
    assert rows[0]["request_to_text"] == pytest.approx(0.2)
    assert rows[0]["text_to_audio"] == pytest.approx(0.3)
    assert rows[0]["request_to_audio"] == pytest.approx(0.5)
    assert rows[0]["foreground_setup"] == ["recv:ready"]
    assert rows[0]["terminal"] == "recv:audio_end"
    assert rows[0]["prior_wait"] is None
    assert rows[0]["actual_gap"] is None
    assert rows[0]["received_bytes"] == rows[0]["emitted_bytes"] == 400

    assert rows[1]["invalid_reason"] == "0 sends claimed this text"
    assert rows[1]["request_to_text"] is None
    assert rows[1]["text_to_audio"] is None
    assert rows[1]["request_to_audio"] is None
    assert rows[1]["context_id"] is None

    assert rows[2]["request_to_text"] == pytest.approx(0.2)
    assert rows[2]["received_bytes"] == rows[2]["emitted_bytes"] == 900
    # Waiting on the previous utterance is reported apart from setup: 3.0 - 0.8.
    assert rows[2]["prior_wait"] == pytest.approx(2.2)
    # Turn 1 never completed, so there is no completion to measure the gap from.
    assert rows[2]["actual_gap"] is None

    # --- each way a single turn fails to qualify -----------------------------
    rows, _, _ = await _replay(_one_turn(), ["one"])
    assert rows[0]["valid"] and rows[0]["terminal_to_stop"] == pytest.approx(0.1)

    cases: list[tuple[dict[str, Any], str]] = [
        ({"sent_ok": False}, "send failed or errored"),
        ({"text_send": False}, "no text reached the wire"),
        ({"audio": False}, "no audio for this turn"),
        # A segment marker is not the utterance's terminal.
        ({"terminal": "recv:segment_end"}, "no terminal message"),
        ({"terminal": None}, "no terminal message"),
        # A terminal on the wire with nothing pushed downstream is the defect
        # this feature fixes; it must not score as a completed utterance.
        ({"stop": False}, "no downstream completion"),
        ({"emitted": 250}, "received 400 bytes but emitted 250 for this context"),
    ]
    for mutation, reason in cases:
        rows, _, _ = await _replay(_one_turn(**mutation), ["one"])
        assert not rows[0]["valid"], f"{mutation} should not qualify"
        assert rows[0]["invalid_reason"] == reason, mutation

    # --- service errors are carried, not swallowed ---------------------------
    _, observer, _ = await _replay(
        _one_turn() + [(1.2, "error", "backend_connection_failed")], ["one"]
    )
    assert observer.errors == ["backend_connection_failed"]

    # --- a cancelled receive loop is not a closed socket ---------------------
    from conftest import FakeWebSocket
    from test_live_smoke import _TimedSocket, _WireLog

    service = _FakeService()
    log = _WireLog(service)
    inner = FakeWebSocket([b"\x01\x02"])
    socket = _TimedSocket(inner, log, log.reserve())
    log.sockets.append(socket)

    stream = socket.__aiter__()
    assert await stream.__anext__() == b"\x01\x02"
    await stream.aclose()  # the service cancels its receive task
    names = [name for _, _, name, _ in log.events]
    assert "recv_end" in names and "closed" not in names
    # Still owned: reserving a replacement while it lives raises the peak.
    log.reserve()
    assert log.peak_owned == 2

    await socket.close()
    assert "closed" in [name for _, _, name, _ in log.events]
    assert socket.closed_state is State.CLOSED

    # Actual batch acceptance, not merely an error present in a side log.
    from test_live_smoke import _qualify_batch, _speak_frames

    async def batch(script):
        rows, observed, wire = await _replay(script, ["one"])
        return dict(
            batch="offline",
            repeat=0,
            rows=rows,
            samples=1,
            stops=len(observed.stops),
            failures=[],
            errors=observed.errors,
            audio_order=observed.audio_order,
            peak_owned=1,
            unclosed=0,
            owned=0,
            owners=(None, None, None),
        )

    valid = await batch(_one_turn())
    _qualify_batch(valid)
    invalid_scripts = [
        [
            (0.7 if kind == "stop" else at, kind, payload)
            for at, kind, payload in _one_turn()
        ],
        _one_turn() + [(1.15, "audio", ("ctx-a", 2))],
        _one_turn() + [(1.2, "stop", "ctx-a")],
        _one_turn() + [(1.2, "error", "backend failed")],
        [event for event in _one_turn() if event[1] != "complete"],
        _one_turn(sent_ok=False),
        _one_turn(text_send=False),
        _one_turn(audio=False),
        _one_turn(terminal=None),
        _one_turn(stop=False),
    ]
    for script in invalid_scripts:
        with pytest.raises(AssertionError):
            _qualify_batch(await batch(script))
    for mutation in (
        {"audio_order": ["ctx-a", "ctx-b", "ctx-a"]},
        {"audio_order": ["wrong"]},
        {"unclosed": 1},
        {"owned": 1},
        {"owners": (object(), None, None)},
        {"peak_owned": 2},
        {"failures": ["deadline"]},
    ):
        with pytest.raises(AssertionError):
            _qualify_batch(valid | mutation)

    # Equal byte totals cannot hide reordered/corrupt PCM.
    rows, observed, wire = await _replay(_one_turn(), ["one"])
    observed.audio_hashes["ctx-a"].update(b"corrupt")
    from test_live_smoke import _spans

    turns = [dict(index=0, context_id="ctx-a", attempt_at=0.45, sent_ok=True)]
    assert not _spans(turns, observed, wire, ["one"])[0]["valid"]
    _, transitions, _ = await _replay(
        [(0.1, "audio", ("a", 2)), (0.2, "audio", ("b", 2)), (0.3, "audio", ("a", 2))],
        [],
    )
    assert transitions.audio_order == ["a", "b", "a"]

    # Wrong-context and duplicate stops do not advance the next request.
    observer = type(observer)(_FakeService(), ["one", "two"])
    turns = [dict(index=0, context_id="a")]
    failures = []
    driver = _speak_frames(["one", "two"], 0, observer, failures, turns)
    assert isinstance(next(driver), TTSSpeakFrame)
    observer.stopped_at["wrong"] = 1
    observer.completed_at["wrong"] = 1
    observer.stops.extend(["wrong", "wrong"])
    assert isinstance(next(driver), SleepFrame)
    observer.stopped_at["a"] = 2
    assert isinstance(next(driver), SleepFrame)
    observer.completed_at["a"] = 2
    assert isinstance(next(driver), TTSSpeakFrame)
    observer.errors.append("failed")
    assert list(driver) == [] and failures

    # Close failures keep ownership; peer close releases it once on every path.
    inner = FakeWebSocket()
    log = _WireLog(_FakeService())
    socket = _TimedSocket(inner, log, log.reserve())
    log.sockets.append(socket)
    real_close = inner.close

    async def failed_close():
        raise RuntimeError("close failed")

    setattr(inner, "close", failed_close)
    with pytest.raises(RuntimeError):
        await socket.close()
    assert log._owned == 1 and socket.closed_state is State.OPEN
    await real_close()  # peer closure, without proxy.close
    assert [message async for message in socket] == []
    assert log._owned == 0
    replacement = log.reserve()
    assert log.peak_owned == 1
    socket.verify_closed()
    assert log._owned == 1
    log.failed_open(replacement)
    assert log._owned == 0


async def test_sarvam_smoke_qualification(monkeypatch):
    """The real smoke body rejects upstream errors, wrong stops and live sockets."""
    import test_live_smoke as smoke
    from conftest import FakeWebSocket
    from pipecat.frames.frames import TranscriptionFrame

    monkeypatch.setenv("SLNG_API_KEY", "offline-placeholder")
    for defect in (
        None,
        "tts_error",
        "stt_error",
        "wrong_stop",
        "early_stop",
        "open",
        "no_socket",
    ):
        captured = []

        async def connect(*args, **kwargs):
            ws = FakeWebSocket()
            captured.append(ws)
            return ws

        monkeypatch.setattr("pipecat_slng.tts.websocket_connect", connect)
        monkeypatch.setattr("pipecat_slng.stt.websocket_connect", connect)

        async def run(service, **kwargs):
            assert kwargs["start_timeout"] == smoke._START_TIMEOUT
            is_tts = isinstance(service, SlngTTSService)
            module = smoke.pipecat_slng.tts if is_tts else smoke.pipecat_slng.stt
            if defect != "no_socket":
                ws = await module.websocket_connect("offline")
                if defect != "open":
                    await ws.close()
            if is_tts:
                audio = TTSAudioRawFrame(
                    audio=b"\x01\x02",
                    sample_rate=16000,
                    num_channels=1,
                    context_id="ctx",
                )
                stop = TTSStoppedFrame(
                    context_id="wrong" if defect == "wrong_stop" else "ctx"
                )
                down = [stop, audio] if defect == "early_stop" else [audio, stop]
            else:
                down = [
                    TranscriptionFrame(
                        text=smoke._SARVAM_SENTENCE, user_id="", timestamp=""
                    )
                ]
            upstream = (
                [ErrorFrame(error="service failed")]
                if defect == ("tts_error" if is_tts else "stt_error")
                else []
            )
            return down, upstream

        monkeypatch.setattr(smoke, "run_test", run)
        if defect is None:
            await smoke.test_live_sarvam_speech()
        else:
            with pytest.raises(AssertionError):
                await smoke.test_live_sarvam_speech()
        for ws in captured:
            await ws.close()


async def _wait_for_connections(fakes, count, timeout=2.0):
    """Wait until `count` connections exist, giving up rather than hanging.

    A missing reconnect is the failure under test, so waiting forever would
    turn a clear assertion into a test-suite timeout.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while len(fakes) < count:
        if asyncio.get_running_loop().time() > deadline:
            return False
        await asyncio.sleep(0.01)
    return True


async def test_quiet_reconnect_emits_one_disconnect(monkeypatch):
    """One per-utterance reconnect fires `on_disconnected` exactly once.

    `on_disconnected` is a public `TTSService` event (pipecat
    `services/tts_service.py:411`), so a consumer's handler runs once per turn
    on every route that reconnects per utterance. `_disconnect_websocket` fires
    it from a `finally`, unconditionally — even when the socket is already
    gone. Any caller that closes the socket itself before the reconnect path
    runs would therefore double the event, which is why that path skips the
    teardown when there is nothing left to tear down.
    """
    from conftest import FakeWebSocket

    fakes: list[FakeWebSocket] = []

    async def _connect(url, **kwargs):
        fake = FakeWebSocket([json.dumps({"type": "ready"})])
        fakes.append(fake)
        return fake

    monkeypatch.setattr("pipecat_slng.tts.websocket_connect", _connect)
    tts = _make_tts()

    disconnects = 0

    @tts.event_handler("on_disconnected")
    async def _count(_service):
        nonlocal disconnects
        disconnects += 1

    # Sampled once the replacement socket exists, so the service's own
    # shutdown teardown at end of test is not counted.
    observed: list[int] = []

    async def drive_one_close():
        if not await _wait_for_connections(fakes, 1):
            return
        if not await _wait_for_text(fakes[0]):
            return
        # The utterance ends and the server closes: the quiet-reconnect path.
        await fakes[0].feed(json.dumps({"type": "audio_end"}))
        await fakes[0].feed(json.dumps({"type": "flushed"}))
        await fakes[0].close()
        await _wait_for_connections(fakes, 2, timeout=3.0)
        await asyncio.sleep(0.05)
        observed.append(disconnects)

    driver = asyncio.create_task(drive_one_close())
    try:
        await run_test(
            tts,
            frames_to_send=[
                TTSSpeakFrame(text="first turn"),
                SleepFrame(sleep=0.4),
                TTSSpeakFrame(text="second turn"),
                SleepFrame(sleep=0.6),
            ],
        )
    finally:
        await asyncio.wait_for(driver, timeout=8)

    assert observed == [1], (
        f"one per-utterance reconnect should fire on_disconnected once, got {observed}"
    )


def _texts_on(fake):
    """Every `text` payload the client sent on this connection."""
    return [
        json.loads(m)["text"]
        for m in fake.sent
        if isinstance(m, str) and json.loads(m).get("type") == "text"
    ]


async def _wait_for_text(fake, timeout=2.0):
    """Wait until the client has sent a `text` message on this connection."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if any(
            isinstance(m, str) and json.loads(m).get("type") == "text"
            for m in fake.sent
        ):
            return True
        await asyncio.sleep(0.01)
    return False


async def test_error_before_ready_delegates_to_base(monkeypatch):
    """A session that never came up is not claimed by quiet recovery.

    Quiet recovery keeps the call alive across a failed turn, but it must not
    swallow a rejected configuration or a bad key: those arrive before `ready`
    and no amount of rebuilding fixes them. Leaving them to the base class
    keeps its backoff and its eventual permanent give-up.
    """
    tts = _make_tts()
    calls: list[str] = []

    async def fake_base(self, error_message, report_error, error=None):
        calls.append(error_message)
        return False

    monkeypatch.setattr(
        "pipecat.services.websocket_service.WebsocketService._maybe_try_reconnect",
        fake_base,
    )

    assert not tts._ready_event.is_set()
    await tts._process_message(
        {"type": "error", "data": {"message": "invalid api key"}}
    )

    assert tts._expect_server_close is False, (
        "an error before ready must not arm the quiet-reconnect flag"
    )
    assert await tts._maybe_try_reconnect("closed", lambda *a, **k: None) is False
    assert calls, "the base class must still handle a session that never came up"


async def test_audio_after_midturn_error_still_plays(monkeypatch):
    """Recovering from an error must not cost the rest of the turn's audio.

    The error usually lands mid-turn, before the reply's audio has arrived.
    `_disconnect_websocket` tears the active audio context down in its
    `finally`, which is right when a turn has ended and wrong here: the turn is
    still live, and everything the rebuilt session delivers for it would be
    discarded. Observed as the agent starting a sentence or two late.
    """
    from conftest import FakeWebSocket

    fakes: list[FakeWebSocket] = []

    async def _connect(url, **kwargs):
        fake = FakeWebSocket([json.dumps({"type": "ready"})])
        fakes.append(fake)
        return fake

    monkeypatch.setattr("pipecat_slng.tts.websocket_connect", _connect)
    tts = _make_tts()
    rest_of_turn = b"\xaa\xbb" * 64

    async def drive():
        if not await _wait_for_connections(fakes, 1):
            return
        if not await _wait_for_text(fakes[0]):
            return
        # Error mid-turn: the text went out, no audio has come back yet.
        await fakes[0].feed(
            json.dumps({"type": "error", "data": {"message": "Stream x not found."}})
        )
        if not await _wait_for_connections(fakes, 2):
            return
        # The rebuilt session delivers the rest of the same turn.
        await fakes[1].feed(rest_of_turn)

    driver = asyncio.create_task(drive())
    try:
        down, _up = await run_test(
            tts,
            frames_to_send=[
                TTSSpeakFrame(text="Of course! I can help you learn physics."),
                SleepFrame(sleep=0.8),
            ],
        )
    finally:
        await asyncio.wait_for(driver, timeout=8)

    audio = [f for f in down if isinstance(f, TTSAudioRawFrame)]
    assert any(f.audio == rest_of_turn for f in audio), (
        f"audio delivered after a mid-turn error was dropped: "
        f"{len(audio)} audio frame(s) reached downstream"
    )


async def test_error_recovery_opens_exactly_one_socket(monkeypatch):
    """Recovery must not open a second, unread connection.

    `run_tts` finding no socket and the receive task's own rebuild both call
    `_connect_websocket`. Unserialised they each open one, but only the socket
    the receive loop picks up is ever read: the utterance sent on the other is
    lost silently, and the next send draws a server-side timeout. Observed in a
    live soniox call as two `Connecting to SLNG TTS` for one `session ready`.
    """
    from conftest import FakeWebSocket

    fakes: list[FakeWebSocket] = []

    async def _connect(url, **kwargs):
        # A real connect is a TLS + WebSocket handshake, not instant. Without
        # that cost there is no window for the two callers to overlap and this
        # check cannot fail.
        await asyncio.sleep(0.25)
        fake = FakeWebSocket([json.dumps({"type": "ready"})])
        fakes.append(fake)
        return fake

    monkeypatch.setattr("pipecat_slng.tts.websocket_connect", _connect)
    tts = _make_tts()

    async def drive():
        if not await _wait_for_connections(fakes, 1):
            return
        if not await _wait_for_text(fakes[0]):
            return
        await fakes[0].feed(
            json.dumps({"type": "error", "data": {"message": "Stream x not found."}})
        )

    driver = asyncio.create_task(drive())
    try:
        await run_test(
            tts,
            frames_to_send=[
                TTSSpeakFrame(text="first"),
                # Lands while the rebuild is still in flight.
                SleepFrame(sleep=0.1),
                TTSSpeakFrame(text="second"),
                SleepFrame(sleep=1.2),
            ],
        )
    finally:
        await asyncio.wait_for(driver, timeout=8)

    assert len(fakes) == 2, (
        f"one error should cost exactly one rebuild (2 connections total); "
        f"opened {len(fakes)} — a spare socket nothing reads swallows an "
        f"utterance"
    )


async def test_healthy_route_keeps_one_session(monkeypatch):
    """A route whose session survives its utterance is never rebuilt.

    `cartesia/sonic:3.5` and the deepgram routes send `audio_end` after every
    turn but keep the session usable, and ran whole calls on one connection
    before any of this. Rebuilding them anyway cost a measured ~440 ms of
    time-to-first-audio per turn — the next turn's first sentence sat on the
    ready gate waiting for a handshake it never needed.
    """
    from conftest import FakeWebSocket

    fakes: list[FakeWebSocket] = []

    async def _connect(url, **kwargs):
        fake = FakeWebSocket([json.dumps({"type": "ready"})])
        fakes.append(fake)
        return fake

    monkeypatch.setattr("pipecat_slng.tts.websocket_connect", _connect)
    tts = _make_tts()
    audio = b"\x05\x06" * 64

    async def drive():
        if not await _wait_for_connections(fakes, 1):
            return
        for turn in range(1, 3):
            # Wait for *this* turn's text, not any text already sent.
            deadline = asyncio.get_running_loop().time() + 3.0
            while len(_texts_on(fakes[0])) < turn:
                if asyncio.get_running_loop().time() > deadline:
                    return
                await asyncio.sleep(0.01)
            await fakes[0].feed(audio)
            await fakes[0].feed(json.dumps({"type": "audio_end", "done": True}))
            await asyncio.sleep(0.15)

    driver = asyncio.create_task(drive())
    try:
        down, up = await run_test(
            tts,
            frames_to_send=[
                TTSSpeakFrame(text="first turn"),
                SleepFrame(sleep=0.5),
                TTSSpeakFrame(text="second turn"),
                SleepFrame(sleep=0.5),
            ],
        )
    finally:
        await asyncio.wait_for(driver, timeout=8)

    assert len(fakes) == 1, (
        f"a healthy route was reconnected {len(fakes) - 1} time(s); each "
        f"rebuild puts a handshake on the next turn's critical path for "
        f"nothing"
    )
    assert "second turn" in _texts_on(fakes[0])
    assert not [f for f in down if isinstance(f, ErrorFrame)]
    assert not [f for f in up if isinstance(f, ErrorFrame)]


async def test_dying_route_recovers_then_rebuilds_between_turns(monkeypatch):
    """A route that loses its session recovers, then pre-empts the next loss.

    `soniox/tts-rt:v1` ends its synthesis stream per utterance and leaves the
    socket open, so the turn after a completed one draws "Stream <id> not
    found. Send a start message first." The first such turn recovers mid-turn
    and still speaks; from then on the session is rebuilt as each turn begins,
    so no later turn pays the error.
    """
    from conftest import FakeWebSocket

    fakes: list[FakeWebSocket] = []

    async def _connect(url, **kwargs):
        fake = FakeWebSocket([json.dumps({"type": "ready"})])
        fakes.append(fake)
        return fake

    monkeypatch.setattr("pipecat_slng.tts.websocket_connect", _connect)
    tts = _make_tts()
    third_audio = b"\x0a\x0b" * 64

    async def drive():
        # Turn 1 speaks; the stream ends but the socket stays open.
        if not await _wait_for_connections(fakes, 1):
            return
        if not await _wait_for_text(fakes[0]):
            return
        await fakes[0].feed(b"\x01\x02" * 64)
        await fakes[0].feed(json.dumps({"type": "audio_end"}))

        # Turn 2 lands on the spent stream and is rejected.
        await _await_sent(fakes[0], "flush", 2)
        await fakes[0].feed(
            json.dumps(
                {
                    "type": "error",
                    "data": {"message": "Stream abc not found."},
                }
            )
        )
        if not await _wait_for_connections(fakes, 2, timeout=3.0):
            return
        await _await_sent(fakes[1], "flush")
        await fakes[1].feed(b"\x05\x06" * 64)
        await fakes[1].feed(json.dumps({"type": "audio_end"}))

        # Turn 3 must land on a session rebuilt before its text went out.
        if not await _wait_for_connections(fakes, 3, timeout=3.0):
            return
        if not await _wait_for_text(fakes[2], timeout=3.0):
            return
        await fakes[2].feed(third_audio)
        await fakes[2].feed(json.dumps({"type": "audio_end"}))

    driver = asyncio.create_task(drive())
    try:
        down, _up = await run_test(
            tts,
            frames_to_send=[
                TTSSpeakFrame(text="turn one"),
                SleepFrame(sleep=0.5),
                TTSSpeakFrame(text="turn two"),
                SleepFrame(sleep=0.5),
                TTSSpeakFrame(text="turn three"),
                SleepFrame(sleep=0.6),
            ],
        )
    finally:
        await asyncio.wait_for(driver, timeout=10)

    assert tts._session_dies_per_utterance, (
        "the failure should have taught the service to rebuild between turns"
    )
    assert len(fakes) >= 3, (
        f"turn three should have been given a session rebuilt at turn start; "
        f"only {len(fakes)} connection(s) were opened"
    )
    assert "turn three" not in _texts_on(fakes[1]), (
        "turn three was sent on the previous turn's spent session"
    )
    assert "turn three" in _texts_on(fakes[2])
    assert any(
        isinstance(f, TTSAudioRawFrame) and f.audio == third_audio for f in down
    ), "the turn after the recovery produced no audio"


async def test_interrupted_turn_still_arms_the_rebuild(monkeypatch):
    """An interrupted turn spends the session, so the next turn must rebuild.

    Observed live: a barge-in ends the turn before `audio_end` arrives, so the
    signal the rebuild keys on never comes. On a route whose stream dies with
    its utterance the session is spent all the same, and the turn after an
    interruption drew "Stream <id> not found" and lost its first sentence —
    that sentence had already gone out before the error came back.
    """
    from conftest import FakeWebSocket

    tts = _make_tts()
    tts._websocket = FakeWebSocket()
    tts._session_dies_per_utterance = True
    tts._expect_server_close = False
    tts._expect_server_close_reason = None

    monkeypatch.setattr(tts, "get_active_audio_context_id", lambda: None)
    await tts.on_audio_context_interrupted("ctx-1")

    assert tts._expect_server_close is True, (
        "an interruption left the spent session unmarked, so the next turn "
        "would be sent into a dead stream"
    )
    assert tts._expect_server_close_reason == "interrupted"


async def test_utterance_lost_to_an_error_is_resent(monkeypatch):
    """The sentence in flight when a session dies is spoken, not dropped.

    Nothing can predict a route's first failure, so one turn per call is sent
    into a session that has already died. Observed live: "Of course!" went out
    44 ms before the error came back and was never heard, while every later
    sentence waited on the ready gate and played. That sentence drew an error
    instead of audio, so replaying it on the replacement adds no duplicate.
    """
    from conftest import FakeWebSocket

    fakes: list[FakeWebSocket] = []

    async def _connect(url, **kwargs):
        fake = FakeWebSocket([json.dumps({"type": "ready"})])
        fakes.append(fake)
        return fake

    monkeypatch.setattr("pipecat_slng.tts.websocket_connect", _connect)
    tts = _make_tts()

    async def drive():
        if not await _wait_for_connections(fakes, 1):
            return
        if not await _wait_for_text(fakes[0]):
            return
        # The session died before it produced a single byte for this text.
        await fakes[0].feed(
            json.dumps({"type": "error", "data": {"message": "Stream x not found."}})
        )
        if not await _wait_for_connections(fakes, 2, timeout=3.0):
            return
        await asyncio.sleep(0.2)
        await fakes[1].feed(b"\x0c\x0d" * 64)

    driver = asyncio.create_task(drive())
    try:
        await run_test(
            tts,
            frames_to_send=[
                TTSSpeakFrame(text="Of course!"),
                SleepFrame(sleep=0.8),
            ],
        )
    finally:
        await asyncio.wait_for(driver, timeout=8)

    assert len(fakes) >= 2, "the dead session was never replaced"
    assert "Of course!" in _texts_on(fakes[1]), (
        f"the utterance that drew the error was never spoken; the "
        f"replacement session only received {_texts_on(fakes[1])}"
    )


async def test_voiced_utterance_is_not_resent(monkeypatch):
    """An utterance that already produced audio is never sent twice.

    The resend exists for text that drew an error instead of audio. If audio
    did come back, replaying the text would speak it a second time.
    """
    from conftest import FakeWebSocket

    fakes: list[FakeWebSocket] = []

    async def _connect(url, **kwargs):
        fake = FakeWebSocket([json.dumps({"type": "ready"})])
        fakes.append(fake)
        return fake

    monkeypatch.setattr("pipecat_slng.tts.websocket_connect", _connect)
    tts = _make_tts()

    async def drive():
        if not await _wait_for_connections(fakes, 1):
            return
        if not await _wait_for_text(fakes[0]):
            return
        # Audio arrives first, so the utterance was spoken.
        await fakes[0].feed(b"\x01\x02" * 64)
        await asyncio.sleep(0.15)
        await fakes[0].feed(
            json.dumps({"type": "error", "data": {"message": "Stream x not found."}})
        )
        await _wait_for_connections(fakes, 2, timeout=3.0)

    driver = asyncio.create_task(drive())
    try:
        await run_test(
            tts,
            frames_to_send=[
                TTSSpeakFrame(text="already spoken"),
                SleepFrame(sleep=0.8),
            ],
        )
    finally:
        await asyncio.wait_for(driver, timeout=8)

    assert len(fakes) >= 2, "the dead session was never replaced"
    assert "already spoken" not in _texts_on(fakes[1]), (
        "an utterance that had already produced audio was spoken twice"
    )


async def test_both_terminal_signals_end_the_turn_once(monkeypatch):
    """A route sending both `audio_end` and `flushed` ends the turn once.

    The two used to be separate branches, only `flushed` closing the audio
    context. They are now one path, so a route that sends both reaches it
    twice — and a second `TTSStoppedFrame` would tell the pipeline the bot
    stopped speaking twice in one turn. Covers the close-after-flush shape
    (`slng/rime/arcana`), which no longer has a reachable route string to test
    against live.
    """
    from conftest import FakeWebSocket

    fakes: list[FakeWebSocket] = []

    async def _connect(url, **kwargs):
        fake = FakeWebSocket([json.dumps({"type": "ready"})])
        fakes.append(fake)
        return fake

    monkeypatch.setattr("pipecat_slng.tts.websocket_connect", _connect)
    tts = _make_tts()

    async def drive():
        if not await _wait_for_connections(fakes, 1):
            return
        if not await _wait_for_text(fakes[0]):
            return
        await fakes[0].feed(b"\x01\x02" * 64)
        await fakes[0].feed(json.dumps({"type": "audio_end"}))
        await fakes[0].feed(json.dumps({"type": "flushed"}))
        # ...and the server closes too, as this shape does.
        await fakes[0].close()
        await _wait_for_connections(fakes, 2, timeout=3.0)

    driver = asyncio.create_task(drive())
    try:
        down, up = await run_test(
            tts,
            frames_to_send=[
                TTSSpeakFrame(text="one turn only"),
                SleepFrame(sleep=0.8),
            ],
        )
    finally:
        await asyncio.wait_for(driver, timeout=8)

    stopped = [f for f in down if isinstance(f, TTSStoppedFrame)]
    assert len(stopped) == 1, (
        f"the turn should end exactly once, got {len(stopped)} "
        f"TTSStoppedFrame(s): 0 means the turn never ends, more than 1 "
        f"reports the bot stopping speaking twice"
    )
    assert not [f for f in down if isinstance(f, ErrorFrame)]
    assert not [f for f in up if isinstance(f, ErrorFrame)]


@pytest.mark.parametrize("second_error", [False, True])
async def test_reconciled_retry_preserves_fragments_flush_and_context(
    monkeypatch, second_error
):
    """PR #8 recovery and ordered ownership retain one whole unvoiced turn."""
    fakes = _collect_sockets(monkeypatch)
    tts = _make_tts()
    payload = b"\x21\x22" * 100

    async def serve():
        await _await_sockets(fakes, 1)
        await _await_sent(fakes[0], "flush")
        await fakes[0].feed(json.dumps({"type": "error", "message": "spent stream"}))
        await _await_sockets(fakes, 2)
        await _await_sent(fakes[1], "flush")
        if second_error:
            await fakes[1].feed(
                json.dumps({"type": "error", "message": "still failed"})
            )
            await _await_sockets(fakes, 3)
        else:
            await fakes[1].feed(payload)
            await fakes[1].feed(json.dumps({"type": "audio_end"}))

    server = asyncio.create_task(serve())
    try:
        down, up = await run_test(
            tts,
            frames_to_send=[
                LLMFullResponseStartFrame(),
                TextFrame("Hello there. "),
                TextFrame("How are you? "),
                TextFrame("Fine thanks. "),
                LLMFullResponseEndFrame(),
                SleepFrame(sleep=0.8),
            ],
        )
    finally:
        await asyncio.wait_for(server, 5)
    assert len(_texts_on(fakes[0])) == 3
    assert _texts_on(fakes[1]) == _texts_on(fakes[0])
    assert _sent_types(fakes[1]).count("flush") == 1
    if second_error:
        assert _texts_on(fakes[2]) == []
        assert not _audio(down) and not _stops(down)
        assert any("abandoned" in f.error for f in up if isinstance(f, ErrorFrame))
    else:
        assert [f.audio for f in _audio(down)] == [payload]
        assert [f.context_id for f in _stops(down)] == [_audio(down)[0].context_id]
    assert all(ws.state is State.CLOSED for ws in fakes)


async def test_reconciled_error_abandons_ambiguous_turns_and_recovers(monkeypatch):
    fakes = _collect_sockets(monkeypatch)
    tts = _make_tts()
    stale, fresh = b"\x31\x32" * 100, b"\x41\x42" * 100

    async def serve():
        await _await_sockets(fakes, 1)
        await _await_sent(fakes[0], "flush", 2)
        await fakes[0].feed(json.dumps({"type": "error", "message": "failed A"}))
        await fakes[0].feed(stale)
        await fakes[0].feed(json.dumps({"type": "audio_end"}))
        await _await_sockets(fakes, 2)
        await _await_sent(fakes[1], "flush")
        await fakes[1].feed(fresh)
        await fakes[1].feed(json.dumps({"type": "audio_end"}))

    server = asyncio.create_task(serve())
    try:
        down, up = await run_test(
            tts,
            frames_to_send=[
                TTSSpeakFrame(text="A"),
                TTSSpeakFrame(text="B"),
                SleepFrame(sleep=0.3),
                TTSSpeakFrame(text="C"),
                SleepFrame(sleep=0.6),
            ],
        )
    finally:
        await asyncio.wait_for(server, 5)
    assert _texts_on(fakes[1]) == ["C"]
    assert [f.audio for f in _audio(down)] == [fresh]
    assert [f.context_id for f in _stops(down)] == [_audio(down)[0].context_id]
    assert (
        len([f for f in up if isinstance(f, ErrorFrame) and "abandoned" in f.error])
        == 2
    )
    assert all(ws.state is State.CLOSED for ws in fakes)


async def test_reconciled_stale_flush_and_control_messages_are_isolated():
    from conftest import FakeWebSocket

    tts = _make_tts()
    old, current = FakeWebSocket(), FakeWebSocket()
    tts._websocket = current
    tts._reserve_wire_turn("A")
    tts._reserve_wire_turn("B")
    await tts.flush_audio("A")
    tts._wire.pop(0)  # normal completion consumes A before playback ends
    await tts.flush_audio("A")
    assert _sent_types(current) == ["flush"]
    for kind in ("ready", "audio_end", "error"):
        await tts._process_message({"type": kind}, old)
    assert not tts._ready_event.is_set()
    assert not tts._expect_server_close
    assert tts._websocket is current
    await tts.flush_audio("B")
    assert _sent_types(current) == ["flush", "flush"]
    # A retired error may have passed its first identity check before waiting
    # for a concurrent replacement to finish opening under the socket lock.
    errors = []

    async def push_error(**kwargs):
        errors.append(kwargs["error_msg"])

    tts.push_error = push_error
    tts._websocket = old
    await tts._connect_lock.acquire()
    delayed = asyncio.create_task(
        tts._process_message({"type": "error", "message": "expired idle session"}, old)
    )
    await asyncio.sleep(0)
    tts._websocket = current
    tts._connect_lock.release()
    await delayed
    assert errors == []
    assert current.state is State.OPEN
    assert _texts_on(current) == []
    await old.close()
    await current.close()


async def test_reconciled_cancellation_during_replacement_closes_old_socket(
    monkeypatch,
):
    from conftest import FakeWebSocket

    connecting = asyncio.Event()
    sockets = []

    async def connect(*args, **kwargs):
        if sockets:
            connecting.set()
            await asyncio.Event().wait()
        ws = FakeWebSocket([json.dumps({"type": "ready"})])
        sockets.append(ws)
        return ws

    monkeypatch.setattr("pipecat_slng.tts.websocket_connect", connect)
    tts = _make_tts()

    def frames():
        yield TTSSpeakFrame(text="no terminal")
        for _ in range(120):
            if connecting.is_set():
                break
            yield SleepFrame(sleep=0.05)
        assert connecting.is_set()
        yield InterruptionFrame()
        yield SleepFrame(sleep=0.2)

    async def serve():
        await _await_sockets(sockets, 1)
        await _await_sent(sockets[0], "flush")
        await sockets[0].feed(b"\x11\x22" * 100)

    server = asyncio.create_task(serve())
    try:
        await asyncio.wait_for(run_test(tts, frames_to_send=frames()), 10)
    finally:
        await asyncio.wait_for(server, 5)
    assert all(ws.state is State.CLOSED for ws in sockets)
    assert (tts._websocket, tts._retiring, tts._receive_task, tts._keepalive_task) == (
        None,
    ) * 4


async def test_reconciled_flush_waits_for_all_replayed_fragments(monkeypatch):
    from conftest import FakeWebSocket

    tts = _make_tts()
    tts._websocket = FakeWebSocket()
    turn = tts._reserve_wire_turn("A")
    turn.text = ["one", "two"]
    tts._wire.clear()
    tts._pending_resend = turn
    current = FakeWebSocket()
    tts._websocket = current
    sending, release = asyncio.Event(), asyncio.Event()
    original = current.send

    async def send(data):
        await original(data)
        if json.loads(data).get("text") == "one":
            sending.set()
            await release.wait()

    monkeypatch.setattr(current, "send", send)
    ready = asyncio.create_task(tts._process_message({"type": "ready"}, current))
    try:
        await asyncio.wait_for(sending.wait(), 2)
        await tts.flush_audio("A")
        assert _sent_types(current) == ["text"]
    finally:
        release.set()
        await asyncio.wait_for(ready, 2)
    assert _texts_on(current) == ["one", "two"]
    assert _sent_types(current) == ["text", "text", "flush"]
    await current.close()


async def test_reconciled_cancel_during_init_closes_acquired_socket(monkeypatch):
    from conftest import FakeWebSocket

    socket = FakeWebSocket()
    sending = asyncio.Event()

    async def connect(*args, **kwargs):
        return socket

    async def send(data):
        sending.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("pipecat_slng.tts.websocket_connect", connect)
    monkeypatch.setattr(socket, "send", send)
    tts = _make_tts()
    connecting = asyncio.create_task(tts._connect_websocket())
    await asyncio.wait_for(sending.wait(), 2)
    connecting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await connecting
    assert socket.state is State.CLOSED
    assert tts._websocket is None


async def test_old_receive_close_preserves_on_demand_replacement(monkeypatch):
    """An old EOF cannot close B's connection or prematurely end its context."""
    from conftest import FakeWebSocket

    fakes = []
    between, finished = asyncio.Event(), asyncio.Event()

    async def connect(*args, **kwargs):
        ws = FakeWebSocket([json.dumps({"type": "ready"})])
        fakes.append(ws)
        return ws

    monkeypatch.setattr("pipecat_slng.tts.websocket_connect", connect)
    tts = _make_tts()

    def frames():
        yield TTSSpeakFrame(text="A")
        while not between.is_set():
            yield SleepFrame(sleep=0.01)
        yield TTSSpeakFrame(text="B")
        while not finished.is_set():
            yield SleepFrame(sleep=0.01)
        yield SleepFrame(sleep=0.2)

    async def server():
        await _await_sockets(fakes, 1)
        await _await_sent(fakes[0], "flush")
        await fakes[0].feed(b"\x11\x22" * 100)
        await fakes[0].feed(json.dumps({"type": "audio_end"}))
        while tts._wire:
            await asyncio.sleep(0.001)
        # The transport is closed before the receive task consumes its EOF.
        fakes[0].state = State.CLOSED
        between.set()
        await _await_sockets(fakes, 2)
        await fakes[0].close()
        await asyncio.sleep(0.05)
        await _await_sent(fakes[-1], "flush")
        await fakes[-1].feed(b"\x33\x44" * 100)
        await fakes[-1].feed(json.dumps({"type": "audio_end"}))
        finished.set()

    task = asyncio.create_task(server())
    try:
        down, up = await asyncio.wait_for(run_test(tts, frames_to_send=frames()), 10)
        await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert len(fakes) == 2
    assert len(_audio(down)) == len(_stops(down)) == 2
    assert not [f.error for f in [*down, *up] if isinstance(f, ErrorFrame)]
    assert all(ws.state is State.CLOSED for ws in fakes)


async def test_failed_on_demand_replacement_keeps_later_speech_receivable(monkeypatch):
    """A failed replacement must not leave a completed receive-task reference."""
    from conftest import FakeWebSocket

    sockets = []
    attempts = 0
    closed, failed, next_turn, finished = (asyncio.Event() for _ in range(4))

    async def connect(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            failed.set()
            raise OSError("temporary connection failure")
        ws = FakeWebSocket([json.dumps({"type": "ready"})])
        sockets.append(ws)
        return ws

    monkeypatch.setattr("pipecat_slng.tts.websocket_connect", connect)
    tts = _make_tts()
    first, last = b"\x11\x22" * 100, b"\x33\x44" * 100

    def frames():
        yield TTSSpeakFrame(text="A")
        while not closed.is_set():
            yield SleepFrame(sleep=0.01)
        yield TTSSpeakFrame(text="B loses its connection")
        while not next_turn.is_set():
            yield SleepFrame(sleep=0.01)
        yield TTSSpeakFrame(text="C still speaks")
        while not finished.is_set():
            yield SleepFrame(sleep=0.01)
        yield SleepFrame(sleep=0.2)

    async def server():
        await _await_sockets(sockets, 1)
        await _await_sent(sockets[0], "flush")
        await sockets[0].feed(first)
        await sockets[0].feed(json.dumps({"type": "audio_end"}))
        while tts._wire:
            await asyncio.sleep(0.001)
        sockets[0].state = State.CLOSED
        closed.set()
        await failed.wait()
        await sockets[0].close()
        # Allow either normal recovery or the buggy receiver exit before C.
        while len(sockets) < 2 and not tts._receive_task.done():
            await asyncio.sleep(0.001)
        next_turn.set()
        await _await_sockets(sockets, 2)
        await _await_sent(sockets[1], "flush", timeout=7)
        await sockets[1].feed(last)
        await sockets[1].feed(json.dumps({"type": "audio_end"}))
        finished.set()

    task = asyncio.create_task(server())
    try:
        down, up = await asyncio.wait_for(run_test(tts, frames_to_send=frames()), 12)
        await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert [f.audio for f in _audio(down)] == [first, last]
    assert [f.context_id for f in _stops(down)] == [f.context_id for f in _audio(down)]
    assert any(
        "temporary connection failure" in f.error
        for f in [*down, *up]
        if isinstance(f, ErrorFrame)
    )
    assert all(ws.state is State.CLOSED for ws in sockets)


@pytest.mark.parametrize("shutdown", [False, True])
async def test_replacement_waits_for_closing_transport(monkeypatch, shutdown):
    """A closing transport remains owned until CLOSED, before the next connect."""
    from conftest import FakeWebSocket

    tts = _make_tts()
    old, replacement = FakeWebSocket(), FakeWebSocket()
    old.state = State.CLOSING
    tts._websocket = old
    release = asyncio.Event()
    opened = []
    real_close = old.close

    async def finish_close():
        await release.wait()
        await real_close()

    async def connect(*args, **kwargs):
        opened.append(old.state)
        return replacement

    monkeypatch.setattr(old, "close", finish_close)
    monkeypatch.setattr("pipecat_slng.tts.websocket_connect", connect)
    task = asyncio.create_task(tts._connect_websocket())
    try:
        await asyncio.sleep(0)
        assert not opened, "replacement started while the old socket was closing"
        tts._disconnecting = shutdown
        release.set()
        await task
        assert opened == ([] if shutdown else [State.CLOSED])
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await real_close()
        await replacement.close()


async def test_old_unfinished_context_cannot_retire_replacement(monkeypatch):
    """Failed A's context timeout cannot truncate speech on B's new socket."""
    from conftest import FakeWebSocket

    sockets = []
    old_dead, finished = asyncio.Event(), asyncio.Event()

    async def connect(*args, **kwargs):
        ws = FakeWebSocket([json.dumps({"type": "ready"})])
        sockets.append(ws)
        return ws

    monkeypatch.setattr("pipecat_slng.tts.websocket_connect", connect)
    tts = _make_tts()
    chunks = [b"\x11\x22" * 100, b"\x33\x44" * 100, b"\x55\x66" * 100]

    def frames():
        yield TTSSpeakFrame(text="A loses its connection")
        while not old_dead.is_set():
            yield SleepFrame(sleep=0.01)
        yield TTSSpeakFrame(text="B still speaks")
        while not finished.is_set():
            yield SleepFrame(sleep=0.01)
        yield SleepFrame(sleep=0.2)

    async def server():
        await _await_sockets(sockets, 1)
        await _await_sent(sockets[0], "flush")
        sockets[0].state = State.CLOSED
        old_dead.set()
        await _await_sockets(sockets, 2)
        await sockets[0].close()
        await _await_sent(sockets[1], "flush")
        await sockets[1].feed(chunks[0])
        await asyncio.sleep(1.6)
        await sockets[1].feed(chunks[1])
        await asyncio.sleep(1.6)  # A would time out; B still has fresh audio.
        await sockets[1].feed(chunks[2])
        await sockets[1].feed(json.dumps({"type": "audio_end"}))
        finished.set()

    task = asyncio.create_task(server())
    try:
        down, up = await asyncio.wait_for(run_test(tts, frames_to_send=frames()), 10)
        await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert [f.audio for f in _audio(down)] == chunks
    assert len(_stops(down)) == 1
    assert {f.context_id for f in _audio(down)} == {_stops(down)[0].context_id}
    assert len(sockets) == 2 and all(ws.state is State.CLOSED for ws in sockets)
    assert any(isinstance(f, ErrorFrame) for f in [*down, *up])  # A fails visibly.


@pytest.mark.parametrize("outcome", ["success", "voiced", "repeated", "multiple"])
async def test_closed_socket_retries_only_one_unvoiced_turn(monkeypatch, outcome):
    """A completed socket dying after B's flush cannot silently lose B."""
    from conftest import FakeWebSocket

    async def ping(self):
        return None

    monkeypatch.setattr(FakeWebSocket, "ping", ping, raising=False)
    fakes = _collect_sockets(monkeypatch)
    tts = _make_tts()
    first, second, last = (bytes([n]) * 200 for n in (11, 22, 33))
    next_turn, recovered, finished = (asyncio.Event() for _ in range(3))

    def frames():
        yield TTSSpeakFrame(text="A")
        while not next_turn.is_set():
            yield SleepFrame(sleep=0.01)
        yield LLMFullResponseStartFrame()
        yield TextFrame("Hello there. ")
        yield TextFrame("How are you? ")
        yield LLMFullResponseEndFrame()
        if outcome == "multiple":
            yield TTSSpeakFrame(text="Also pending")
        while not recovered.is_set():
            yield SleepFrame(sleep=0.01)
        yield TTSSpeakFrame(text="C")
        while not finished.is_set():
            yield SleepFrame(sleep=0.01)
        yield SleepFrame(sleep=0.2)

    async def serve():
        await _await_sockets(fakes, 1)
        await _await_sent(fakes[0], "flush")
        await fakes[0].feed(first)
        await fakes[0].feed(json.dumps({"type": "audio_end"}))
        while tts._wire:
            await asyncio.sleep(0.001)
        next_turn.set()
        await _await_sent(fakes[0], "flush", 3 if outcome == "multiple" else 2)
        if outcome == "voiced":
            # Queue audio immediately before EOF: it must be consumed before
            # deciding whether the turn is eligible for replay.
            await fakes[0].feed(second)
        await fakes[0].close()
        await _await_sockets(fakes, 2)
        if outcome in ("success", "repeated"):
            # On the unfixed adapter this times out: B has been discarded.
            await _await_sent(fakes[1], "flush")
            if outcome == "success":
                await fakes[1].feed(second)
                await fakes[1].feed(json.dumps({"type": "audio_end"}))
                while tts._wire:
                    await asyncio.sleep(0.001)
            else:
                await fakes[1].close()
                await _await_sockets(fakes, 3)
        recovered.set()
        while not any("C" in _texts_on(ws) for ws in fakes[1:]):
            await asyncio.sleep(0.001)
        current = next(ws for ws in fakes[1:] if "C" in _texts_on(ws))
        await _await_sent(current, "flush")
        await current.feed(last)
        await current.feed(json.dumps({"type": "audio_end"}))
        finished.set()

    server = asyncio.create_task(serve())
    try:
        down, up = await asyncio.wait_for(run_test(tts, frames_to_send=frames()), 8)
        await server
    finally:
        server.cancel()
        await asyncio.gather(server, return_exceptions=True)
    original = _texts_on(fakes[0])[1:]
    replays = [_texts_on(ws) for ws in fakes[1:]]
    if outcome in ("success", "repeated"):
        assert original == ["Hello there.", "How are you?"]
        assert replays[0] == original
        assert not any(text in original for sends in replays[1:] for text in sends)
    else:
        assert not any(text in original for sends in replays for text in sends)
    expected = (
        [first, second, last] if outcome in ("success", "voiced") else [first, last]
    )
    assert [f.audio for f in _audio(down)] == expected
    stops = [f.context_id for f in _stops(down)]
    assert len(stops) == len(set(stops)) == (3 if outcome == "success" else 2)
    assert bool([f for f in [*down, *up] if isinstance(f, ErrorFrame)]) == (
        outcome != "success"
    )
    assert all(ws.state is State.CLOSED for ws in fakes)
    assert tts._websocket is tts._receive_task is tts._keepalive_task is None


async def test_completed_idle_session_error_reconnects_on_next_request(monkeypatch):
    """Provider idle expiry neither fails completed speech nor opens idle loops."""
    fakes = _collect_sockets(monkeypatch)
    tts = _make_tts()
    expired, finished = asyncio.Event(), asyncio.Event()
    first, second = b"\x11\x22" * 2400, b"\x33\x44" * 100

    def frames():
        yield TTSSpeakFrame(text="A")
        while not expired.is_set():
            yield SleepFrame(sleep=0.01)
        yield TTSSpeakFrame(text="B")
        while not finished.is_set():
            yield SleepFrame(sleep=0.01)
        yield SleepFrame(sleep=0.2)

    async def serve():
        await _await_sockets(fakes, 1)
        await _await_sent(fakes[0], "flush")
        await fakes[0].feed(first)
        await fakes[0].feed(json.dumps({"type": "audio_end"}))
        # The error follows the completed synthesis while playback still drains.
        await fakes[0].feed(
            json.dumps(
                {
                    "type": "error",
                    "message": "Session produced no output for 120 seconds",
                }
            )
        )
        await asyncio.sleep(0.2)
        assert len(fakes) == 1, "idle expiry must not open another idle session"
        assert fakes[0].state is State.CLOSED
        expired.set()
        await _await_sockets(fakes, 2)
        await _await_sent(fakes[1], "flush")
        await fakes[1].feed(second)
        await fakes[1].feed(json.dumps({"type": "audio_end"}))
        finished.set()

    server = asyncio.create_task(serve())
    try:
        down, up = await asyncio.wait_for(run_test(tts, frames_to_send=frames()), 5)
        await server
    finally:
        server.cancel()
        await asyncio.gather(server, return_exceptions=True)
    assert [f.audio for f in _audio(down)] == [first, second]
    assert [f.context_id for f in _stops(down)] == [f.context_id for f in _audio(down)]
    assert not [f.error for f in [*down, *up] if isinstance(f, ErrorFrame)]
    assert len(fakes) == 2 and all(ws.state is State.CLOSED for ws in fakes)
    assert tts._websocket is tts._receive_task is tts._keepalive_task is None


@pytest.mark.parametrize("enabled", [None, False, True])
async def test_tts_warm_standby_selection(monkeypatch, enabled):
    """A ready spare takes the whole next utterance before old close finishes."""
    from conftest import FakeWebSocket
    from loguru import logger

    sockets, outcomes = [], []
    release_close, next_turn, finished = (asyncio.Event() for _ in range(3))
    maximum = 0

    async def connect(*args, **kwargs):
        nonlocal maximum
        assert sum(ws.state is not State.CLOSED for ws in sockets) < (
            2 if enabled else 1
        )
        ws = FakeWebSocket([json.dumps({"type": "ready"})])
        sockets.append(ws)
        maximum = max(maximum, sum(s.state is not State.CLOSED for s in sockets))
        if enabled and len(sockets) == 1:
            close = ws.close

            async def slow_close():
                ws.state = State.CLOSING
                await release_close.wait()
                await close()

            monkeypatch.setattr(ws, "close", slow_close)
        return ws

    monkeypatch.setattr("pipecat_slng.tts.websocket_connect", connect)
    options = {} if enabled is None else {"warm_standby_enabled": enabled}
    tts = SlngTTSService(
        api_key="test-key", voice="voice", sample_rate=24000, **options
    )
    sink = logger.add(
        lambda message: (
            outcomes.append(message.record["extra"]["slng_warm_standby"])
            if "slng_warm_standby" in message.record["extra"]
            else None
        )
    )
    first, second = b"\x11\x22" * 100, b"\x33\x44" * 100

    def frames():
        yield LLMFullResponseStartFrame()
        yield TextFrame("First sentence. ")
        yield TextFrame("Second sentence. ")
        yield LLMFullResponseEndFrame()
        while not next_turn.is_set():
            yield SleepFrame(sleep=0.01)
        yield TTSSpeakFrame(text="Next utterance")
        while not finished.is_set():
            yield SleepFrame(sleep=0.01)
        yield SleepFrame(sleep=0.15)

    async def serve():
        try:
            await _await_sockets(sockets, 1)
            await _await_sent(sockets[0], "flush")
            if enabled:
                await _await_sockets(sockets, 2)
                # Let the spare's ready pass through its own reader.
                await asyncio.sleep(0.03)
                assert _texts_on(sockets[1]) == []
            await sockets[0].feed(first)
            await sockets[0].feed(json.dumps({"type": "audio_end"}))
            next_turn.set()
            target = sockets[1] if enabled else sockets[0]
            await _await_sent(target, "flush", 1 if enabled else 2)
            if enabled:
                assert sockets[0].state is State.CLOSING
                assert len(sockets) == 2
                assert _texts_on(sockets[0]) == ["First sentence.", "Second sentence."]
                assert _texts_on(target) == ["Next utterance"]
            await target.feed(second)
            await target.feed(json.dumps({"type": "audio_end"}))
        finally:
            release_close.set()
            finished.set()

    server = asyncio.create_task(serve())
    try:
        down, up = await asyncio.wait_for(run_test(tts, frames_to_send=frames()), 8)
        await server
    finally:
        release_close.set()
        server.cancel()
        await asyncio.gather(server, return_exceptions=True)
        logger.remove(sink)
    assert [f.audio for f in _audio(down)] == [first, second]
    assert [f.context_id for f in _stops(down)] == [f.context_id for f in _audio(down)]
    assert not [f for f in [*down, *up] if isinstance(f, ErrorFrame)]
    assert maximum == (2 if enabled else 1)
    assert all(ws.state is State.CLOSED for ws in sockets)
    assert len(outcomes) == 2
    assert outcomes[0]["miss_reason"] == (
        "initial_connection" if enabled else "disabled"
    )
    assert outcomes[1]["used"] is bool(enabled)
    if enabled:
        assert outcomes[1]["miss_reason"] is None
        assert outcomes[1]["prepared_ms"] >= 0


@pytest.mark.parametrize(
    "failure",
    ["pending", "not_ready", "failed", "quota", "closed", "audio", "terminal"],
)
async def test_tts_warm_standby_fallback(monkeypatch, failure):
    """Unavailable preparation never delays otherwise working active speech."""
    from conftest import FakeWebSocket
    from loguru import logger

    sockets, outcomes = [], []
    opening, release, next_turn, finished = (asyncio.Event() for _ in range(4))
    attempts = 0

    async def connect(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            opening.set()
            if failure == "pending":
                await release.wait()
            if failure in ("failed", "quota"):
                raise ConnectionError(
                    "quota denied" if failure == "quota" else "test connection failure"
                )
        assert sum(ws.state is not State.CLOSED for ws in sockets) < 2
        initial = (
            []
            if attempts == 2 and failure == "not_ready"
            else [json.dumps({"type": "ready"})]
        )
        ws = FakeWebSocket(initial)
        sockets.append(ws)
        if attempts == 2:
            if failure == "closed":
                await ws.close()
            elif failure == "audio":
                await ws.feed(b"unexpected standby audio")
            elif failure == "terminal":
                await ws.feed(json.dumps({"type": "audio_end"}))
        return ws

    monkeypatch.setattr("pipecat_slng.tts.websocket_connect", connect)
    tts = SlngTTSService(
        api_key="secret-test-key",
        voice="voice",
        sample_rate=24000,
        warm_standby_enabled=True,
    )
    sink = logger.add(
        lambda message: (
            outcomes.append(message.record["extra"]["slng_warm_standby"])
            if "slng_warm_standby" in message.record["extra"]
            else None
        )
    )
    payloads = [b"\x01\x02" * 100, b"\x03\x04" * 100]

    def frames():
        yield TTSSpeakFrame(text="private first speech")
        while not next_turn.is_set():
            yield SleepFrame(sleep=0.01)
        yield TTSSpeakFrame(text="private second speech")
        while not finished.is_set():
            yield SleepFrame(sleep=0.01)
        yield SleepFrame(sleep=0.15)

    async def serve():
        await _await_sockets(sockets, 1)
        await _await_sent(sockets[0], "flush")
        await opening.wait()
        await asyncio.sleep(0.03)
        await sockets[0].feed(payloads[0])
        await sockets[0].feed(json.dumps({"type": "audio_end"}))
        next_turn.set()
        await _await_sent(sockets[0], "flush", 2)
        # The second text reached the active wire before pending preparation
        # was allowed to connect or obtain its ready acknowledgement.
        assert not release.is_set()
        await sockets[0].feed(payloads[1])
        await sockets[0].feed(json.dumps({"type": "audio_end"}))
        release.set()
        finished.set()

    server = asyncio.create_task(serve())
    try:
        down, up = await asyncio.wait_for(run_test(tts, frames_to_send=frames()), 8)
        await server
    finally:
        release.set()
        server.cancel()
        await asyncio.gather(server, return_exceptions=True)
        logger.remove(sink)
    assert [f.audio for f in _audio(down)] == payloads
    assert len(_stops(down)) == 2
    assert not [f for f in [*down, *up] if isinstance(f, ErrorFrame)]
    expected = failure if failure in ("pending", "not_ready", "closed") else "failed"
    assert outcomes[1]["miss_reason"] == expected and not outcomes[1]["used"]
    assert "private" not in json.dumps(
        outcomes
    ) and "secret-test-key" not in json.dumps(outcomes)
    assert attempts <= 3
    assert all(ws.state is State.CLOSED for ws in sockets)


@pytest.mark.parametrize("completion", ["terminal", "timeout"])
async def test_tts_warm_standby_audio_isolation(monkeypatch, completion):
    """B waits for A's final audio, then uses a spare while A playback drains."""
    fakes = _collect_sockets(monkeypatch)
    tts = SlngTTSService(
        api_key="test-key",
        voice="voice",
        sample_rate=24000,
        warm_standby_enabled=True,
        stop_frame_timeout_s=0.1 if completion == "timeout" else 3.0,
    )
    first, tail, second = (bytes([n]) * 100 for n in (11, 22, 33))

    async def serve():
        await _await_sockets(fakes, 2)
        await _await_sent(fakes[0], "flush")
        await fakes[0].feed(first)
        await asyncio.sleep(0.03)
        assert _texts_on(fakes[0]) == ["A first.", "A last."]
        assert _texts_on(fakes[1]) == []
        await fakes[0].feed(tail)
        if completion == "terminal":
            await fakes[0].feed(json.dumps({"type": "audio_end"}))
        await _await_sent(fakes[1], "flush")
        # A delayed control callback and trailing transport traffic have no
        # authority over the new connection's context or readiness.
        await tts._process_message({"type": "audio_end"}, fakes[0])
        await tts._handle_audio_bytes(b"stale", fakes[0])
        await fakes[1].feed(second)
        await fakes[1].feed(json.dumps({"type": "audio_end"}))

    server = asyncio.create_task(serve())
    try:
        down, up = await run_test(
            tts,
            frames_to_send=[
                LLMFullResponseStartFrame(),
                TextFrame("A first. "),
                TextFrame("A last. "),
                LLMFullResponseEndFrame(),
                TTSSpeakFrame(text="B"),
                SleepFrame(sleep=0.4),
            ],
        )
        await server
    finally:
        server.cancel()
        await asyncio.gather(server, return_exceptions=True)
    assert [f.audio for f in _audio(down)] == [first, tail, second]
    contexts = [f.context_id for f in _audio(down)]
    assert contexts[0] == contexts[1] != contexts[2]
    assert [f.context_id for f in _stops(down)] == (
        [contexts[0], contexts[2]] if completion == "terminal" else [contexts[2]]
    )
    errors = [f for f in [*down, *up] if isinstance(f, ErrorFrame)]
    assert bool(errors) is (completion == "timeout")
    assert all(ws.state is State.CLOSED for ws in fakes)


@pytest.mark.parametrize("phase", ["opening", "ready", "noop"])
async def test_tts_warm_standby_settings(monkeypatch, phase):
    """Changing effective settings invalidates only obsolete preparations."""
    from conftest import FakeWebSocket

    sockets, calls = [], []
    entered, next_turn, finished = (asyncio.Event() for _ in range(3))
    pronunciation = {"name": "before"}

    async def connect(url, **kwargs):
        call = len(calls)
        calls.append((url, dict(kwargs["additional_headers"])))
        if call == 1 and phase == "opening":
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # An acquired socket returned late still belongs to the canceled
                # opener and must be closed, never published with stale settings.
                pass
        ws = FakeWebSocket([json.dumps({"type": "ready"})])
        sockets.append(ws)
        if call == 1:
            entered.set()
        return ws

    monkeypatch.setattr("pipecat_slng.tts.websocket_connect", connect)
    tts = SlngTTSService(
        api_key="test-key",
        model="gradium/tts:default",
        voice="before",
        sample_rate=24000,
        language=Language.EN,
        pronunciation=pronunciation,
        region_override="eu-north-1",
        provider_key="provider-test-key",
        warm_standby_enabled=True,
    )

    def frames():
        yield TTSSpeakFrame(text="A")
        while not next_turn.is_set():
            yield SleepFrame(sleep=0.01)
        yield TTSSpeakFrame(text="B")
        while not finished.is_set():
            yield SleepFrame(sleep=0.01)
        yield SleepFrame(sleep=0.15)

    async def serve():
        await _await_sockets(sockets, 1)
        await _await_sent(sockets[0], "flush")
        await entered.wait()
        await sockets[0].feed(b"\x11\x22" * 100)
        await sockets[0].feed(json.dumps({"type": "audio_end"}))
        while tts._wire:
            await asyncio.sleep(0.001)
        await asyncio.sleep(0.03)
        if phase == "noop":
            await tts._update_settings(SlngTTSSettings(voice="before"))
            assert len(calls) == 2
        else:
            await tts._update_settings(
                SlngTTSSettings(
                    voice="after",
                    language=Language.HI,
                    speed=1.1,
                    pronunciation={"name": "after"},
                )
            )
            assert sockets[1].state is State.CLOSED
            assert _texts_on(sockets[1]) == []
        next_turn.set()
        while not any("B" in _texts_on(ws) for ws in sockets):
            await asyncio.sleep(0.001)
        target = next(ws for ws in sockets if "B" in _texts_on(ws))
        await _await_sent(target, "flush")
        init = json.loads(target.sent[0])
        assert init["voice"] == ("before" if phase == "noop" else "after")
        if phase != "noop":
            assert init["config"]["language"] == "hi"
            assert init["config"]["speed"] == 1.1
            assert init["config"]["pronunciation"] == {"name": "after"}
        assert all(url.endswith("/gradium/tts:default") for url, _ in calls)
        assert all(
            headers
            == {
                "Authorization": "Bearer test-key",
                "X-Region-Override": "eu-north-1",
                "X-Slng-Provider-Key": "provider-test-key",
            }
            for _, headers in calls
        )
        await target.feed(b"\x33\x44" * 100)
        await target.feed(json.dumps({"type": "audio_end"}))
        finished.set()

    server = asyncio.create_task(serve())
    try:
        down, up = await asyncio.wait_for(run_test(tts, frames_to_send=frames()), 8)
        await server
    finally:
        server.cancel()
        await asyncio.gather(server, return_exceptions=True)
    assert [f.audio for f in _audio(down)] == [b"\x11\x22" * 100, b"\x33\x44" * 100]
    assert len(_stops(down)) == 2
    assert not [f for f in [*down, *up] if isinstance(f, ErrorFrame)]
    assert all(ws.state is State.CLOSED for ws in sockets)


@pytest.mark.parametrize("phase", ["opening", "ready", "promotion"])
@pytest.mark.parametrize("ending", ["stop", "cancel"])
async def test_tts_warm_standby_cleanup(monkeypatch, phase, ending):
    """All sockets remain owned through shutdown, including handoff cancellation."""
    from conftest import FakeWebSocket
    from pipecat.frames.frames import CancelFrame

    sockets = []
    entered, first_done, promoting = (asyncio.Event() for _ in range(3))

    async def connect(*args, **kwargs):
        if len(sockets) == 1 and phase == "opening":
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                pass
        ws = FakeWebSocket([json.dumps({"type": "ready"})])
        sockets.append(ws)
        if len(sockets) == 2:
            entered.set()
        return ws

    monkeypatch.setattr("pipecat_slng.tts.websocket_connect", connect)
    tts = SlngTTSService(
        api_key="test-key", voice="voice", sample_rate=24000, warm_standby_enabled=True
    )
    prepare = tts._prepare_standby

    async def delayed_prepare(generation):
        try:
            await prepare(generation)
        finally:
            if phase == "promotion" and not tts._disconnecting:
                promoting.set()
                await asyncio.sleep(0.1)

    monkeypatch.setattr(tts, "_prepare_standby", delayed_prepare)

    def frames():
        yield TTSSpeakFrame(text="A")
        while not first_done.is_set():
            yield SleepFrame(sleep=0.01)
        if phase == "promotion":
            yield TTSSpeakFrame(text="B")
            while not promoting.is_set():
                yield SleepFrame(sleep=0.01)
        if ending == "cancel":
            yield CancelFrame()

    async def serve():
        await _await_sockets(sockets, 1)
        await _await_sent(sockets[0], "flush")
        await entered.wait()
        await sockets[0].feed(b"\x11\x22" * 100)
        await sockets[0].feed(json.dumps({"type": "audio_end"}))
        await asyncio.sleep(0.03)
        first_done.set()
        if phase == "promotion" and ending == "stop":
            await _await_sent(sockets[1], "flush")
            await sockets[1].feed(b"\x33\x44" * 100)
            await sockets[1].feed(json.dumps({"type": "audio_end"}))

    server = asyncio.create_task(serve())
    try:
        await asyncio.wait_for(
            run_test(tts, frames_to_send=frames(), send_end_frame=ending == "stop"), 8
        )
        await server
    finally:
        server.cancel()
        await asyncio.gather(server, return_exceptions=True)
    await tts.cleanup()
    await tts.cleanup()
    assert all(ws.state is State.CLOSED for ws in sockets)
    assert tts._websocket is tts._standby_ws is tts._standby_retiring is None
    assert tts._receive_task is tts._keepalive_task is tts._standby_task is None
    assert tts._standby_close_task is None or tts._standby_close_task.done()
    count = len(sockets)
    await asyncio.sleep(0.03)
    assert len(sockets) == count


@pytest.mark.parametrize("standby", [False, True])
async def test_tts_warm_standby_failed_close_ownership(monkeypatch, standby):
    """Failed init/close retains ownership, and cannot prevent active teardown."""
    from conftest import FakeWebSocket

    tts = SlngTTSService(api_key="test", warm_standby_enabled=True)
    active, broken = FakeWebSocket(), FakeWebSocket()
    close = broken.close

    async def fail(*args, **kwargs):
        raise ConnectionError("injected failure")

    async def connect(*args, **kwargs):
        return broken

    monkeypatch.setattr("pipecat_slng.tts.websocket_connect", connect)
    monkeypatch.setattr(broken, "send", fail)
    monkeypatch.setattr(broken, "close", fail)
    with pytest.raises(ConnectionError):
        await tts._open_socket(tts._connection_options(), standby=standby)
    assert broken.state is State.OPEN
    assert (tts._standby_ws if standby else tts._websocket) is broken
    if standby:
        tts._websocket = active
        with pytest.raises(ConnectionError):
            await tts._disconnect()
        assert active.state is State.CLOSED
        assert tts._standby_ws is broken
    monkeypatch.setattr(broken, "close", close)
    # The failed init is still owned and can be closed on the next cleanup.
    await tts.cleanup()
    assert broken.state is State.CLOSED
    assert tts._websocket is tts._standby_ws is None


async def test_tts_warm_standby_buffered_audio(monkeypatch):
    """Queued unsolicited audio invalidates a ready spare at handoff."""
    from conftest import FakeWebSocket

    tts = SlngTTSService(api_key="test", warm_standby_enabled=True)
    active = FakeWebSocket()
    spare = FakeWebSocket([json.dumps({"type": "ready"})])
    tts._websocket = active
    monkeypatch.setattr(tts, "create_task", asyncio.create_task)

    async def connect(*args, **kwargs):
        return spare

    monkeypatch.setattr("pipecat_slng.tts.websocket_connect", connect)
    tts._start_standby()
    while not tts._standby_ready:
        await asyncio.sleep(0)
    await spare.feed(b"unsolicited before any text")
    used, reason, _ = await tts._select_standby()
    assert not used and reason == "failed"
    assert tts._websocket is active and active.state is State.OPEN
    assert spare.state is State.CLOSED and not _texts_on(spare)
    await tts.cleanup()


async def test_tts_replacement_waits_for_own_ready(monkeypatch):
    """A prior ready cannot let replacement text bypass the init handshake."""
    from conftest import FakeWebSocket

    tts = SlngTTSService(api_key="test")
    old, new = FakeWebSocket(), FakeWebSocket()
    await old.close()
    tts._websocket = old
    tts._ready_event.set()

    async def connect(*args, **kwargs):
        return new

    monkeypatch.setattr("pipecat_slng.tts.websocket_connect", connect)
    await tts._connect_websocket()
    task = asyncio.create_task(anext(tts.run_tts("Hello", "context")))
    await asyncio.sleep(0.02)
    assert not _texts_on(new)
    await tts._process_message({"type": "ready"}, new)
    assert await task is None
    assert _texts_on(new) == ["Hello"]
    await tts.cleanup()


async def test_measurement_standby_qualification():
    """A spare cannot supply another socket's setup, audio, or claimed hit."""
    from test_live_smoke import _spans

    rows, observed, wire = await _replay(
        sorted(
            _one_turn()
            + [
                (0.35, "spare_wire", "connect_start"),
                (0.46, "spare_wire", "recv:ready"),
                (0.55, "spare_wire", ("audio", 800)),
                (0.6, "spare_wire", "recv:audio_end"),
            ]
        ),
        ["one"],
    )
    assert rows[0]["valid"]
    assert rows[0]["foreground_setup"] == ["recv:ready"]
    assert rows[0]["received_bytes"] == 400
    assert rows[0]["request_to_audio"] == pytest.approx(0.5)
    turns = [dict(index=0, context_id="ctx-a", attempt_at=0.45, sent_ok=True)]
    # A claimed hit cannot borrow readiness from the other socket.
    wire.selections["ctx-a"] = dict(enabled=True, used=True, at=0.49)
    wire.events = [
        event
        for event in wire.events
        if not (event[1] == 1 and event[2] == "recv:ready")
    ]
    assert not _spans(turns, observed, wire, ["one"])[0]["valid"]
    # A genuinely ready speech socket has no foreground setup in this window.
    wire.events.insert(0, (0.25, 1, "recv:ready", 0))
    assert _spans(turns, observed, wire, ["one"])[0]["valid"]


async def test_tts_warm_standby_expected_eof_and_keepalive(monkeypatch):
    """Keepalive failure is isolated; completed EOF needs no redundant opener."""
    from conftest import FakeWebSocket

    tts = SlngTTSService(api_key="test", warm_standby_enabled=True)
    active, spare = FakeWebSocket(), FakeWebSocket()
    tts._websocket, tts._standby_ws = active, spare
    tts._standby_ready = True
    tts._standby_options = tts._connection_options()
    monkeypatch.setattr(tts, "create_task", asyncio.create_task)

    async def cancel_task(task):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    monkeypatch.setattr(tts, "cancel_task", cancel_task)
    monkeypatch.setattr("pipecat_slng.tts._KEEPALIVE_INTERVAL", 0.01)

    async def fail_send(*args, **kwargs):
        raise ConnectionError("active keepalive failed")

    async def unexpected_connect(*args, **kwargs):
        pytest.fail("a completed EOF must leave the ready spare selectable")

    monkeypatch.setattr(active, "send", fail_send)
    monkeypatch.setattr("pipecat_slng.tts.websocket_connect", unexpected_connect)
    tts._keepalive_task = asyncio.create_task(tts._keepalive_task_handler())
    await _await_sent(spare, "keepalive", count=2)
    await active.close()
    tts._receiving_socket = active
    tts._expect_server_close = True
    assert not await tts._maybe_try_reconnect("completed EOF", None)
    await spare.feed(json.dumps({"type": "ready"}))  # duplicate, buffered at handoff
    used, reason, _ = await tts._select_standby()
    assert used and reason is None and tts._websocket is spare
    unready = FakeWebSocket()

    async def replenish(*args, **kwargs):
        return unready

    monkeypatch.setattr("pipecat_slng.tts.websocket_connect", replenish)
    tts._start_standby()
    await _await_sent(unready, "init")
    # The promoted socket's duplicate ack cannot qualify this new candidate.
    used, reason, _ = await tts._select_standby()
    assert not used and reason == "not_ready"
    await tts.cleanup()
    assert unready.state is State.CLOSED
    assert active.state is spare.state is State.CLOSED


async def test_tts_warm_standby_ambiguous_send_is_not_replayed(monkeypatch):
    """Acceptance followed by a send exception cannot duplicate speech."""
    from conftest import FakeWebSocket

    sockets = []
    tts = SlngTTSService(api_key="test", sample_rate=24000, warm_standby_enabled=True)

    async def connect(*args, **kwargs):
        ws = FakeWebSocket([json.dumps({"type": "ready"})])
        sockets.append(ws)
        if len(sockets) == 2:
            send = ws.send

            async def accepted(data):
                await send(data)
                if json.loads(data).get("type") == "text":
                    raise ConnectionError("accepted then disconnected")

            monkeypatch.setattr(ws, "send", accepted)
        return ws

    monkeypatch.setattr("pipecat_slng.tts.websocket_connect", connect)

    async def serve():
        await _await_sockets(sockets, 2)
        await _await_sent(sockets[0], "flush")
        await sockets[0].feed(b"\x11\x22" * 100)
        await sockets[0].feed(json.dumps({"type": "audio_end"}))

    server = asyncio.create_task(serve())
    try:
        down, up = await run_test(
            tts,
            frames_to_send=[
                TTSSpeakFrame(text="first"),
                SleepFrame(sleep=0.15),
                TTSSpeakFrame(text="accepted once"),
                SleepFrame(sleep=0.2),
            ],
        )
        await server
    finally:
        server.cancel()
        await asyncio.gather(server, return_exceptions=True)
    assert [text for ws in sockets for text in _texts_on(ws)].count(
        "accepted once"
    ) == 1
    assert any(isinstance(f, ErrorFrame) for f in [*down, *up])
    assert [f.audio for f in _audio(down)] == [b"\x11\x22" * 100]
    assert all(ws.state is State.CLOSED for ws in sockets)


async def test_tts_warm_standby_mutable_settings_during_selection(monkeypatch):
    """Init is a snapshot; settings changed during reader join invalidate it."""
    from conftest import FakeWebSocket

    pronunciation = {"name": "before"}
    tts = SlngTTSService(
        api_key="test", pronunciation=pronunciation, warm_standby_enabled=True
    )
    active, spare = FakeWebSocket(), FakeWebSocket()
    tts._websocket = active
    monkeypatch.setattr(tts, "create_task", asyncio.create_task)

    async def connect(*args, **kwargs):
        return spare

    monkeypatch.setattr("pipecat_slng.tts.websocket_connect", connect)
    options = tts._connection_options()
    await tts._open_socket(options, standby=True)
    tts._standby_options, tts._standby_ready = options, True
    watching = asyncio.Event()

    async def watcher():
        try:
            watching.set()
            await asyncio.Event().wait()
        finally:
            pronunciation["name"] = "after"

    tts._standby_task = asyncio.create_task(watcher())
    await watching.wait()
    used, reason, _ = await tts._select_standby()
    assert not used and reason == "settings_changed"
    assert json.loads(spare.sent[0])["config"]["pronunciation"] == {"name": "before"}
    assert json.loads(tts._connection_options()[2])["config"]["pronunciation"] == {
        "name": "after"
    }
    assert spare.state is State.CLOSED and tts._websocket is active
    await tts.cleanup()
