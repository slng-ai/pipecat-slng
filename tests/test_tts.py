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


async def test_expected_close_reports_arming_event(patch_ws, monkeypatch):
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

    # A failed turn arms the same flag, so the reconnect log says which of the
    # three reasons drove it. A branch nothing can observe must not ship.
    tts._expect_server_close = False
    tts._expect_server_close_reason = None

    from conftest import FakeWebSocket

    doomed = FakeWebSocket()
    tts._websocket = doomed
    tts._ready_event.set()

    await tts._process_message({"type": "error", "data": {"message": "boom"}})
    assert tts._expect_server_close is True
    assert tts._expect_server_close_reason == "error"
    assert not tts._ready_event.is_set()
    # The failed session is actually dropped, not just flagged.
    assert doomed.state is State.CLOSED
    assert tts._websocket is None


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
# Session recovery: a spent or failed session is rebuilt, so one bad turn
# cannot mute the rest of the call.
# ---------------------------------------------------------------------------


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
        if not await _wait_for_text(fakes[0], timeout=3.0):
            return
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
        await fakes[1].feed(json.dumps({"type": "audio_end"}))

        # Turn 3 must land on a session rebuilt before its text went out.
        if not await _wait_for_connections(fakes, 3, timeout=3.0):
            return
        if not await _wait_for_text(fakes[2], timeout=3.0):
            return
        await fakes[2].feed(third_audio)

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
    tts = _make_tts()
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
