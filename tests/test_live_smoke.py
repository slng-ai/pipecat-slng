#
# Copyright (c) 2026, slng.ai
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Live smoke tests against wss://api.slng.ai.

Skipped unless SLNG_API_KEY is set. These hit the real bridge, so they are
excluded from offline/CI-without-secrets runs.
"""

import asyncio
import importlib.metadata
import json
import os
import statistics
import time

import pytest
from pipecat.frames.frames import (
    InputAudioRawFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSSpeakFrame,
    TTSStoppedFrame,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed
from pipecat.tests.utils import SleepFrame, run_test

import pipecat_slng
import pipecat_slng.tts
from pipecat_slng import SlngHttpTTSService, SlngSTTService, SlngTTSService

# Marked, not name-matched: `-k 'not live'` also deselects anything whose name
# merely contains "live" — it silently dropped all three keepaLIVE tests.
pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.getenv("SLNG_API_KEY"), reason="SLNG_API_KEY not set"),
]


async def test_live_tts_returns_audio():
    """Real TTS bridge returns audio for a short utterance."""
    tts = SlngTTSService(
        api_key=os.environ["SLNG_API_KEY"],
        model="slng/deepgram/aura:2-en",
        voice="aura-2-thalia-en",
        sample_rate=24000,
    )

    down, _ = await run_test(
        tts,
        frames_to_send=[TTSSpeakFrame(text="Hello from SLNG."), SleepFrame(sleep=3.0)],
    )

    assert any(isinstance(f, TTSAudioRawFrame) and f.audio for f in down)


async def test_live_stt_connects_and_finalizes():
    """Real STT bridge accepts audio without erroring; transcript optional."""
    stt = SlngSTTService(
        api_key=os.environ["SLNG_API_KEY"],
        model="slng/deepgram/nova:3-en",
        sample_rate=16000,
    )

    silence = b"\x00\x00" * 8000
    down, _ = await run_test(
        stt,
        frames_to_send=[
            InputAudioRawFrame(audio=silence, sample_rate=16000, num_channels=1),
            SleepFrame(sleep=3.0),
        ],
    )
    # Connecting + handshake without raising is the real check (run_test would
    # have raised on failure). Any transcripts that did arrive must carry text.
    transcripts = [f for f in down if isinstance(f, TranscriptionFrame)]
    assert all(f.text for f in transcripts)


# Route 2 (BYOK): external route + your own provider key, billed upstream.
# Gated on generic env so any provider works (V22) — populate with e.g. deepgram:
#   SLNG_PROVIDER_KEY, SLNG_BYOK_STT_MODEL, SLNG_BYOK_TTS_MODEL[, SLNG_BYOK_TTS_VOICE].
byok = pytest.mark.skipif(
    not (
        os.getenv("SLNG_PROVIDER_KEY")
        and os.getenv("SLNG_BYOK_STT_MODEL")
        and os.getenv("SLNG_BYOK_TTS_MODEL")
    ),
    reason="BYOK env not set (SLNG_PROVIDER_KEY + SLNG_BYOK_STT_MODEL + SLNG_BYOK_TTS_MODEL)",
)


@byok
async def test_live_byok_tts_returns_audio():
    """Route 2: external WS-TTS route + provider_key returns audio, billed upstream."""
    tts = SlngTTSService(
        api_key=os.environ["SLNG_API_KEY"],
        model=os.environ["SLNG_BYOK_TTS_MODEL"],
        voice=os.getenv("SLNG_BYOK_TTS_VOICE", "aura-2-thalia-en"),
        sample_rate=24000,
        provider_key=os.environ["SLNG_PROVIDER_KEY"],
    )

    down, _ = await run_test(
        tts,
        frames_to_send=[TTSSpeakFrame(text="Hello from BYOK."), SleepFrame(sleep=3.0)],
    )

    assert any(isinstance(f, TTSAudioRawFrame) and f.audio for f in down)


@byok
async def test_live_byok_stt_connects_and_finalizes():
    """Route 2: external STT route + provider_key accepts audio without erroring."""
    stt = SlngSTTService(
        api_key=os.environ["SLNG_API_KEY"],
        model=os.environ["SLNG_BYOK_STT_MODEL"],
        sample_rate=16000,
        provider_key=os.environ["SLNG_PROVIDER_KEY"],
    )

    silence = b"\x00\x00" * 8000
    down, _ = await run_test(
        stt,
        frames_to_send=[
            InputAudioRawFrame(audio=silence, sample_rate=16000, num_channels=1),
            SleepFrame(sleep=3.0),
        ],
    )
    transcripts = [f for f in down if isinstance(f, TranscriptionFrame)]
    assert all(f.text for f in transcripts)


@byok
async def test_live_byok_http_tts_returns_audio():
    """Route 2: external HTTP TTS route + provider_key returns audio."""
    tts = SlngHttpTTSService(
        api_key=os.environ["SLNG_API_KEY"],
        model=os.environ["SLNG_BYOK_TTS_MODEL"],
        voice=os.getenv("SLNG_BYOK_TTS_VOICE", "aura-2-thalia-en"),
        sample_rate=24000,
        provider_key=os.environ["SLNG_PROVIDER_KEY"],
    )

    down, _ = await run_test(
        tts,
        frames_to_send=[
            TTSSpeakFrame(text="Hello from BYOK over HTTP."),
            SleepFrame(sleep=3.0),
        ],
    )

    assert any(isinstance(f, TTSAudioRawFrame) and f.audio for f in down)


# Route 3: external route WITHOUT a provider key — proxied via SLNG's own
# provider account, billed by SLNG (V21). Needs only SLNG_API_KEY (module mark).
_EXTERNAL_TTS_MODEL = "deepgram/aura:2"
_EXTERNAL_STT_MODEL = "deepgram/nova:3"


async def test_live_route3_external_tts_returns_audio():
    """Route 3 (WS TTS): external route, no provider_key, served by SLNG's account."""
    tts = SlngTTSService(
        api_key=os.environ["SLNG_API_KEY"],
        model=_EXTERNAL_TTS_MODEL,
        voice="aura-2-thalia-en",
        sample_rate=24000,
    )

    down, _ = await run_test(
        tts,
        frames_to_send=[
            TTSSpeakFrame(text="Hello from an external route."),
            SleepFrame(sleep=3.0),
        ],
    )

    assert any(isinstance(f, TTSAudioRawFrame) and f.audio for f in down)


async def test_live_route3_external_stt_connects_and_finalizes():
    """Route 3 (STT): external route, no provider_key, served by SLNG's account."""
    stt = SlngSTTService(
        api_key=os.environ["SLNG_API_KEY"],
        model=_EXTERNAL_STT_MODEL,
        sample_rate=16000,
    )

    silence = b"\x00\x00" * 8000
    down, _ = await run_test(
        stt,
        frames_to_send=[
            InputAudioRawFrame(audio=silence, sample_rate=16000, num_channels=1),
            SleepFrame(sleep=3.0),
        ],
    )
    transcripts = [f for f in down if isinstance(f, TranscriptionFrame)]
    assert all(f.text for f in transcripts)


async def test_live_route3_external_http_tts_returns_audio():
    """Route 3 (HTTP TTS): external route, no provider_key, served by SLNG's account."""
    tts = SlngHttpTTSService(
        api_key=os.environ["SLNG_API_KEY"],
        model=_EXTERNAL_TTS_MODEL,
        voice="aura-2-thalia-en",
        sample_rate=24000,
    )

    down, _ = await run_test(
        tts,
        frames_to_send=[
            TTSSpeakFrame(text="Hello from an external route over HTTP."),
            SleepFrame(sleep=3.0),
        ],
    )

    assert any(isinstance(f, TTSAudioRawFrame) and f.audio for f in down)


async def test_live_http_tts_returns_audio():
    """Real HTTP TTS bridge returns audio for a short utterance."""
    tts = SlngHttpTTSService(
        api_key=os.environ["SLNG_API_KEY"],
        model="slng/deepgram/aura:2-en",
        voice="aura-2-thalia-en",
        sample_rate=24000,
    )

    down, _ = await run_test(
        tts,
        frames_to_send=[
            TTSSpeakFrame(text="Hello from SLNG over HTTP."),
            SleepFrame(sleep=3.0),
        ],
    )

    assert any(isinstance(f, TTSAudioRawFrame) and f.audio for f in down)


# ---------------------------------------------------------------------------
# Warm-standby baseline measurement (spec 001-tts-warm-standby, US1)
#
# Gated behind SLNG_TTS_MEASURE=1 on top of SLNG_API_KEY: it synthesises
# repeated batches against the real bridge, so an ordinary live smoke run must
# not pick it up. Route and voice are explicit test-only inputs, never
# defaulted — a measurement of an unnamed route proves nothing about the route
# a deployment actually uses.
# ---------------------------------------------------------------------------

# Experiment inputs, not production defaults. The idle gap deliberately spans
# the service's inherited 30s keepalive interval; the caller gap is a plausible
# pause between turns; the immediate batch requests the next utterance as soon
# as the previous one completes.
_MEASURE_BATCHES = (
    ("immediate", 0.0, 5),
    ("caller-gap", 1.5, 5),
    ("idle-gap", 35.0, 2),
)
_MEASURE_REPEATS = 2
_MEASURE_TEXTS = (
    "The northbound platform is closing for maintenance.",
    "Your appointment has been moved to Thursday morning.",
    "I can look that up for you right now.",
    "There are two seats left on the later train.",
    "Let me know if you would like the receipt emailed.",
)
_UTTERANCE_DEADLINE = 15.0
_POLL_INTERVAL = 0.02
# Harness limits, not service timeouts. run_test defaults to a one-second
# startup deadline, which a real TLS handshake plus WebSocket upgrade plus init
# does not fit inside; the measured startup span is reported from the wire log
# instead of being asserted against this deadline.
_START_TIMEOUT = 20.0

measurement = pytest.mark.skipif(
    os.getenv("SLNG_TTS_MEASURE") != "1"
    or not os.getenv("SLNG_TTS_MODEL")
    or not os.getenv("SLNG_TTS_VOICE"),
    reason="needs SLNG_TTS_MEASURE=1 plus explicit SLNG_TTS_MODEL and SLNG_TTS_VOICE",
)


def _wire_kind(message) -> str:
    """Name a wire frame for the event log without recording its payload."""
    if isinstance(message, bytes):
        return "audio" if message else "audio_empty"
    try:
        kind = json.loads(message).get("type")
    except (json.JSONDecodeError, AttributeError):
        return "recv:non_json"
    return f"recv:{str(kind).lower() or 'unknown'}"


class _WireLog:
    """Monotonic wire-event log shared by every socket of one measurement run.

    Records only event names and timestamps: no speech text, no credentials,
    no headers. ``peak_live`` is the high-water mark of sockets that have
    connected and not yet finished closing, which is the resource-ownership
    bound the specification cares about.
    """

    def __init__(self):
        """Start an empty log with no sockets seen."""
        self.events: list[tuple[float, int, str]] = []
        self.sockets = 0
        self._live = 0
        self.peak_live = 0

    def mark(self, socket_no: int, label: str) -> float:
        """Append one timestamped event and return its timestamp."""
        now = time.perf_counter()
        self.events.append((now, socket_no, label))
        return now

    def opened(self, socket_no: int):
        """Count a socket as owned from the moment it connects."""
        self._live += 1
        self.peak_live = max(self.peak_live, self._live)
        self.mark(socket_no, "open")

    def retired(self, socket_no: int):
        """Release a socket's ownership once it is fully closed."""
        self._live -= 1
        self.mark(socket_no, "retired")

    def first(self, label: str, after: float) -> float | None:
        """Timestamp of the first ``label`` at or after ``after``, else None."""
        return next(
            (at for at, _, name in self.events if name == label and at >= after), None
        )

    def last(self, label: str, before: float) -> float | None:
        """Timestamp of the last ``label`` strictly before ``before``, else None."""
        return next(
            (
                at
                for at, _, name in reversed(self.events)
                if name == label and at < before
            ),
            None,
        )

    def between(self, start: float, end: float) -> list[str]:
        """Event names observed in ``[start, end]``, in order."""
        return [name for at, _, name in self.events if start <= at <= end]


class _TimedSocket:
    """Timestamping proxy around one live bridge WebSocket.

    Delegates everything the service and the Pipecat base class use — ``state``,
    ``ping``, ``recv`` — through ``__getattr__``, and intercepts only the three
    operations that carry timing: sending, iterating received frames, and
    closing.
    """

    def __init__(self, inner, log: _WireLog, socket_no: int):
        """Wrap ``inner``, recording its events into ``log`` as ``socket_no``."""
        self._inner = inner
        self._log = log
        self._no = socket_no
        self._retired = False

    def __getattr__(self, name):
        """Delegate every unintercepted attribute to the real connection."""
        return getattr(self._inner, name)

    def _retire_once(self):
        """Release ownership exactly once, however the socket ended."""
        if not self._retired:
            self._retired = True
            self._log.retired(self._no)

    async def send(self, data):
        """Timestamp the outbound message kind, then send it."""
        try:
            kind = json.loads(data).get("type") if isinstance(data, str) else "binary"
        except json.JSONDecodeError:
            kind = "non_json"
        self._log.mark(self._no, f"send:{str(kind).lower()}")
        await self._inner.send(data)

    async def close(self, *args, **kwargs):
        """Timestamp both ends of the close so closing time stays visible."""
        self._log.mark(self._no, "close_start")
        try:
            await self._inner.close(*args, **kwargs)
        finally:
            self._log.mark(self._no, "close_done")
            self._retire_once()

    async def __aiter__(self):
        """Yield received frames, timestamping each one by kind."""
        try:
            async for message in self._inner:
                self._log.mark(self._no, _wire_kind(message))
                yield message
        finally:
            self._retire_once()


def _install_wire_log(monkeypatch) -> _WireLog:
    """Wrap the TTS module's connector so every socket is timestamped."""
    log = _WireLog()
    real_connect = pipecat_slng.tts.websocket_connect

    async def _connect(url, **kwargs):
        log.sockets += 1
        socket_no = log.sockets
        log.mark(socket_no, "connect_start")
        inner = await real_connect(url, **kwargs)
        log.mark(socket_no, "connect_done")
        log.opened(socket_no)
        return _TimedSocket(inner, log, socket_no)

    monkeypatch.setattr("pipecat_slng.tts.websocket_connect", _connect)
    return log


class _TurnObserver(BaseObserver):
    """Timestamp requests into ``tts`` and audio/completion out of it.

    Observers are awaited inline inside ``push_frame``, so the arrival of a
    speak frame at the service is a true ordered instant. The ``on_tts_request``
    event is not usable for that: Pipecat dispatches async event handlers as
    tasks, so a timestamp taken inside one can land *after* the text has
    already gone out on the wire. That hook is still used, for the ordered
    context IDs it carries.

    Filtering on the processor keeps each frame counted once; the pipeline
    pushes the same frame between several processor pairs.
    """

    def __init__(self, tts, completed: asyncio.Event):
        """Watch frames into and out of ``tts``, signalling ``completed``."""
        super().__init__()
        self._tts = tts
        self._completed = completed
        self.requested_at: list[float] = []
        self.audio_order: list[str] = []
        self.audio_bytes: dict[str, int] = {}
        self.stopped: list[str] = []

    async def on_push_frame(self, data: FramePushed):
        """Record request arrival, per-context audio, and terminal frames."""
        frame = data.frame
        if data.destination is self._tts:
            if isinstance(frame, TTSSpeakFrame):
                self.requested_at.append(time.perf_counter())
            return
        if data.source is not self._tts:
            return
        if isinstance(frame, TTSAudioRawFrame) and frame.audio:
            ctx = frame.context_id or ""
            if ctx not in self.audio_bytes:
                self.audio_order.append(ctx)
            self.audio_bytes[ctx] = self.audio_bytes.get(ctx, 0) + len(frame.audio)
        elif isinstance(frame, TTSStoppedFrame):
            self.stopped.append(frame.context_id or "")
            self._completed.set()


def _speak_frames(texts, gap: float, completed: asyncio.Event):
    """Yield one speak frame per text, waiting for each to complete first.

    ponytail: polls with short sleep frames because ``run_test``'s frame pump
    is a plain loop with no hook to await on. Upgrade path: drive the pipeline
    worker directly if a finer boundary is ever needed. The per-utterance
    deadline is a harness limit, not a service timeout: an utterance that
    exceeds it is reported as incomplete rather than silently overlapped.
    """
    for text in texts:
        completed.clear()
        yield TTSSpeakFrame(text=text)
        started = time.perf_counter()
        while (
            not completed.is_set()
            and time.perf_counter() - started < _UTTERANCE_DEADLINE
        ):
            yield SleepFrame(sleep=_POLL_INTERVAL)
        if gap:
            yield SleepFrame(sleep=gap)


_SETUP_EVENTS = ("connect_start", "connect_done", "send:init", "recv:ready")
_TERMINAL_EVENTS = ("recv:flushed", "recv:audio_end", "recv:error", "close_done")


def _spans(requested_at, context_ids, log: _WireLog) -> list[dict]:
    """Derive per-utterance spans by pairing requests with their text sends.

    Paired by position rather than by timestamp: one speak frame produces one
    ``text`` message, and pairing by position is immune to the sub-millisecond
    ordering noise between an inline observer and a task-dispatched event.

    Each row carries request-to-text, text-to-first-audio and their combined
    request-to-first-audio, the setup events that actually landed in the
    foreground window before the text went out, and the terminal events the
    bridge sent for that utterance. A span that cannot be observed stays
    ``None`` rather than becoming a fabricated zero.

    ``prior_wait`` is kept separate from the setup spans: it is the gap between
    the previous utterance's last audio on the wire and this request, so time
    spent waiting for the preceding utterance is never read as preparation for
    this one.
    """
    text_times = [at for at, _, name in log.events if name == "send:text"]
    rows = []
    for index, request_at in enumerate(requested_at):
        prior_audio_at = log.last("audio", request_at) if index else None
        row: dict = {
            "index": index,
            "context_id": context_ids[index] if index < len(context_ids) else None,
            "request_to_text": None,
            "text_to_audio": None,
            "request_to_audio": None,
            "prior_wait": None
            if prior_audio_at is None
            else request_at - prior_audio_at,
            "foreground_setup": [],
            "terminal": [],
        }
        rows.append(row)

        if index >= len(text_times):
            continue
        text_at = text_times[index]
        next_text_at = (
            text_times[index + 1] if index + 1 < len(text_times) else float("inf")
        )
        row["request_to_text"] = text_at - request_at
        row["foreground_setup"] = [
            name for name in log.between(request_at, text_at) if name in _SETUP_EVENTS
        ]
        row["terminal"] = [
            name
            for name in log.between(text_at, next_text_at)
            if name in _TERMINAL_EVENTS
        ]

        audio_at = log.first("audio", text_at)
        if audio_at is None or audio_at > next_text_at:
            continue
        row["text_to_audio"] = audio_at - text_at
        row["request_to_audio"] = audio_at - request_at
    return rows


def _median(values) -> float | None:
    """Median of the observed values, or None when nothing was observed."""
    observed = [v for v in values if v is not None]
    return statistics.median(observed) if observed else None


def _tail(values) -> float | None:
    """Slowest observed value, or None when nothing was observed.

    The worst of a handful of samples, reported as the observed tail. It is not
    a population percentile and the specification forbids presenting it as one.
    """
    observed = [v for v in values if v is not None]
    return max(observed) if observed else None


def _fmt(value) -> str:
    """Render a span in milliseconds, or ``n/a`` when unobserved."""
    return "n/a" if value is None else f"{value * 1000:.0f}ms"


async def _measure_batch(monkeypatch, name: str, gap: float, samples: int) -> dict:
    """Run one baseline batch and return its recorded spans and outcomes."""
    log = _install_wire_log(monkeypatch)
    tts = SlngTTSService(
        api_key=os.environ["SLNG_API_KEY"],
        model=os.environ["SLNG_TTS_MODEL"],
        voice=os.environ["SLNG_TTS_VOICE"],
        sample_rate=24000,
    )

    context_ids: list[str] = []

    @tts.event_handler("on_tts_request")
    async def _on_tts_request(service, context_id: str, text: str):
        context_ids.append(context_id)

    completed = asyncio.Event()
    observer = _TurnObserver(tts, completed)
    texts = [_MEASURE_TEXTS[i % len(_MEASURE_TEXTS)] for i in range(samples)]

    down, _ = await run_test(
        tts,
        frames_to_send=_speak_frames(texts, gap, completed),
        observers=[observer],
        start_timeout=_START_TIMEOUT,
    )

    startup = log.first("connect_start", 0.0)
    ready = log.first("recv:ready", 0.0)

    return {
        "batch": name,
        "gap": gap,
        "samples": samples,
        "rows": _spans(observer.requested_at, context_ids, log),
        "startup_to_ready": None
        if startup is None or ready is None
        else ready - startup,
        "sockets": log.sockets,
        "peak_live": log.peak_live,
        "completed": len(observer.stopped),
        "audio_bytes": observer.audio_bytes,
        "audio_order": [
            f.context_id for f in down if isinstance(f, TTSAudioRawFrame) and f.audio
        ],
        "owners": (tts._websocket, tts._receive_task, tts._keepalive_task),
    }


@measurement
async def test_live_tts_warm_standby_measurement(monkeypatch):
    """Measure the existing preparation baseline on an explicitly named route.

    Baseline only: this build has no standby option, so the batches here answer
    one question — after startup connection, keepalive and eager reconnection,
    is there still session setup sitting in front of an utterance's text? Each
    batch is repeated so a later comparison can be judged against baseline
    variation rather than against a single run.

    Asserts the outcomes a consumer depends on (complete, correctly ordered
    audio and no owned connections or tasks after teardown) and reports the
    timings and terminal-event sequence as evidence.
    """
    results = []
    for repeat in range(_MEASURE_REPEATS):
        for name, gap, samples in _MEASURE_BATCHES:
            result = await _measure_batch(monkeypatch, name, gap, samples)
            result["repeat"] = repeat
            results.append(result)

    print(f"\n=== TTS baseline measurement: {os.environ['SLNG_TTS_MODEL']} ===")
    print(
        f"pipecat {importlib.metadata.version('pipecat-ai')}, {pipecat_slng.__file__}"
    )
    for result in results:
        rows = result["rows"]
        print(
            f"\n[{result['batch']} r{result['repeat']}] "
            f"gap={result['gap']}s n={len(rows)} "
            f"sockets={result['sockets']} peak_owned={result['peak_live']} "
            f"completed={result['completed']}/{result['samples']} "
            f"startup_connect_to_ready={_fmt(result['startup_to_ready'])}"
        )
        for span in ("request_to_text", "text_to_audio", "request_to_audio"):
            values = [r[span] for r in rows]
            print(
                f"  {span}: median {_fmt(_median(values))}, "
                f"observed tail {_fmt(_tail(values))}, "
                f"n={sum(1 for v in values if v is not None)}"
            )
        for row in rows:
            print(
                f"  #{row['index']} request->text {_fmt(row['request_to_text'])} "
                f"text->audio {_fmt(row['text_to_audio'])} "
                f"request->audio {_fmt(row['request_to_audio'])} "
                f"prior_wait {_fmt(row['prior_wait'])} "
                f"foreground_setup={row['foreground_setup'] or 'none'} "
                f"terminal={row['terminal'] or 'none'}"
            )
        if result["completed"] < result["samples"]:
            print(
                "  NOTE: not every utterance produced a terminal stop frame — "
                "completion may be falling through to a timeout (research decision 3)"
            )

    for result in results:
        label = f"{result['batch']} r{result['repeat']}"
        assert result["audio_order"], f"{label}: no audio at all"
        assert len(result["audio_bytes"]) == result["samples"], (
            f"{label}: {len(result['audio_bytes'])} contexts produced audio, "
            f"expected {result['samples']}"
        )
        # Audio for one context must finish before the next context's begins;
        # a context reappearing later means interleaved or stale playback.
        seen: list[str] = []
        for context_id in result["audio_order"]:
            if not seen or seen[-1] != context_id:
                assert context_id not in seen, (
                    f"{label}: audio for {context_id} resumed"
                )
                seen.append(context_id)
        assert result["peak_live"] <= 1, (
            f"{label}: baseline owned {result['peak_live']} simultaneous sockets"
        )
        assert result["owners"] == (None, None, None), (
            f"{label}: resources still owned after teardown: {result['owners']}"
        )
