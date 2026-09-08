#
# Copyright (c) 2026, slng.ai
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Live smoke tests against wss://api.slng.ai.

Skipped unless SLNG_API_KEY is set. These hit the real bridge, so they are
excluded from offline/CI-without-secrets runs.
"""

import importlib.metadata
import json
import os
import statistics

import pytest
from pipecat.frames.frames import (
    ErrorFrame,
    InputAudioRawFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSSpeakFrame,
    TTSStoppedFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed
from pipecat.services.settings import NOT_GIVEN, NotGiven
from pipecat.tests.utils import SleepFrame, run_test
from pipecat.transcriptions.language import Language
from websockets.protocol import State

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
# not pick it up. Route, voice and language are explicit test-only inputs, never
# defaulted — a measurement of an unnamed route proves nothing about the route
# a deployment actually uses.
#
# What a batch may conclude is bounded by what it can show. `request->text` is
# the adapter handing text over, not evidence about provider preparation: a
# small value says nothing about setup happening behind the client socket after
# the text is submitted. Only a sample whose send, audio, terminal and
# downstream completion are all accounted for is reported as a timing
# observation at all.
# ---------------------------------------------------------------------------

# Experiment inputs, not production defaults. The idle gap deliberately spans
# the service's inherited 30s keepalive interval; the caller gap is a plausible
# pause between turns; the immediate batch requests the next utterance as soon
# as the previous one completes. Four idle samples keep at least three
# subsequent-utterance samples in every batch.
_MEASURE_BATCHES = (
    ("immediate", 0.0, 5),
    ("caller-gap", 1.5, 5),
    ("idle-gap", 35.0, 4),
)
_MEASURE_REPEATS = 2
# Distinct per batch, so a turn's text identifies it without the log ever
# recording speech.
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

# Captured at import, before any batch wraps it, so repeated per-batch
# instrumentation cannot nest wrappers on top of each other.
_REAL_WS_CONNECT = pipecat_slng.tts.websocket_connect

measurement = pytest.mark.skipif(
    os.getenv("SLNG_TTS_MEASURE") != "1"
    or not os.getenv("SLNG_TTS_MODEL")
    or not os.getenv("SLNG_TTS_VOICE"),
    reason="needs SLNG_TTS_MEASURE=1 plus explicit SLNG_TTS_MODEL and SLNG_TTS_VOICE",
)


def _measure_language() -> Language | NotGiven:
    """Validate the optional test-only language input.

    Test input only: the service's own default is untouched. An unrecognised
    value fails the run rather than quietly synthesising in another language.
    """
    raw = os.getenv("SLNG_TTS_LANGUAGE")
    if not raw:
        return NOT_GIVEN
    try:
        return Language(raw)
    except ValueError as exc:  # pragma: no cover - depends on operator input
        raise AssertionError(
            f"SLNG_TTS_LANGUAGE={raw!r} is not a pipecat Language value"
        ) from exc


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
    """Wire events timestamped on the pipeline's own clock.

    Frame timestamps come from ``FramePushed.timestamp``, which the pipeline
    stamps in nanoseconds off its clock before queuing. Wire events are read
    from that same clock here, so the two are directly comparable; the previous
    ``time.perf_counter()`` reading mixed two origins and could put a text send
    *before* the request that caused it.

    Records only event names, timestamps and audio byte counts: no speech text,
    no credentials, no headers. Ownership is counted from the reservation taken
    before connecting until a socket is verified closed, so a cancelled receive
    loop cannot make a live connection disappear from the count.
    """

    def __init__(self, tts):
        """Read time from ``tts``'s pipeline clock."""
        self._tts = tts
        self.events: list[tuple[float, int, str, int]] = []
        self.sockets: list = []
        self._owned = 0
        self.peak_owned = 0

    def now(self) -> float:
        """Seconds on the pipeline clock."""
        return self._tts.get_clock().get_time() / 1e9

    def mark(self, socket_no: int, label: str, nbytes: int = 0) -> float:
        """Append one timestamped event and return its timestamp."""
        at = self.now()
        self.events.append((at, socket_no, label, nbytes))
        return at

    def reserve(self) -> int:
        """Own a socket from before the connect attempt, and number it."""
        socket_no = len(self.sockets) + 1
        self._owned += 1
        self.peak_owned = max(self.peak_owned, self._owned)
        self.mark(socket_no, "connect_start")
        return socket_no

    def failed_open(self, socket_no: int):
        """Release a reservation whose connect never produced a socket."""
        self._owned -= 1
        self.mark(socket_no, "connect_failed")

    def closed(self, socket_no: int):
        """Release ownership once the socket is actually closed."""
        self._owned -= 1
        self.mark(socket_no, "closed")

    def first(
        self, label: str, start: float, end: float = float("inf")
    ) -> float | None:
        """Timestamp of the first ``label`` in ``[start, end)``, else None."""
        return next(
            (
                at
                for at, _, name, _ in self.events
                if name == label and start <= at < end
            ),
            None,
        )

    def first_of(self, labels, start: float, end: float = float("inf")):
        """First ``(timestamp, name)`` from ``labels`` in ``[start, end)``."""
        return next(
            (
                (at, name)
                for at, _, name, _ in self.events
                if name in labels and start <= at < end
            ),
            (None, None),
        )

    def last(self, label: str, before: float) -> float | None:
        """Timestamp of the last ``label`` strictly before ``before``, else None."""
        return next(
            (
                at
                for at, _, name, _ in reversed(self.events)
                if name == label and at < before
            ),
            None,
        )

    def names(self, start: float, end: float) -> list[str]:
        """Event names observed in ``[start, end)``, in order."""
        return [name for at, _, name, _ in self.events if start <= at < end]

    def audio_bytes(self, start: float, end: float) -> int:
        """Audio bytes received off the wire in ``[start, end)``."""
        return sum(
            nbytes
            for at, _, name, nbytes in self.events
            if name == "audio" and start <= at < end
        )


class _TimedSocket:
    """Timestamping proxy around one live bridge WebSocket.

    Delegates everything the service and the Pipecat base class use — ``state``,
    ``ping``, ``recv`` — through ``__getattr__``, and intercepts only the three
    operations that carry timing: sending, iterating received frames, and
    closing.

    Ending the receive iteration is recorded but does not release ownership: the
    service cancels that task while the socket is still open, and counting it as
    closed there is what let a batch report zero live connections with one still
    connected.
    """

    def __init__(self, inner, log: _WireLog, socket_no: int):
        """Wrap ``inner``, recording its events into ``log`` as ``socket_no``."""
        self._inner = inner
        self._log = log
        self._no = socket_no
        self._closed = False

    def __getattr__(self, name):
        """Delegate every unintercepted attribute to the real connection."""
        return getattr(self._inner, name)

    @property
    def closed_state(self):
        """The real connection's state, for post-teardown assertions."""
        return self._inner.state

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
            if not self._closed:
                self._closed = True
                self._log.closed(self._no)

    async def __aiter__(self):
        """Yield received frames, timestamping each one by kind and size."""
        try:
            async for message in self._inner:
                self._log.mark(
                    self._no,
                    _wire_kind(message),
                    len(message) if isinstance(message, bytes) else 0,
                )
                yield message
        finally:
            self._log.mark(self._no, "recv_end")


def _install_wire_log(mp, tts) -> _WireLog:
    """Wrap the TTS module's connector so every socket is timestamped."""
    log = _WireLog(tts)

    async def _connect(url, **kwargs):
        socket_no = log.reserve()
        try:
            inner = await _REAL_WS_CONNECT(url, **kwargs)
        except Exception:
            log.failed_open(socket_no)
            raise
        socket = _TimedSocket(inner, log, socket_no)
        log.sockets.append(socket)
        log.mark(socket_no, "open")
        return socket

    mp.setattr("pipecat_slng.tts.websocket_connect", _connect)
    return log


def _install_turn_log(tts, log: _WireLog, texts) -> list[dict]:
    """Record each turn's context ID, send attempt and send outcome together.

    ``run_tts`` is the one place where a turn's text and its synthesis context
    ID are visible at once, and it runs on the task that performs the send — so
    an attempt recorded here cannot be reordered against the wire event it
    causes. Pairing requests to text sends by position, as this harness used to,
    silently shifted every later utterance onto the wrong request as soon as one
    send went missing.
    """
    index_of = {text: i for i, text in enumerate(texts)}
    real_run_tts = tts.run_tts
    turns: list[dict] = []

    async def _run_tts(text: str, context_id: str):
        turn = {
            "index": index_of.get(text.strip()),
            "context_id": context_id,
            "attempt_at": log.now(),
            "sent_ok": True,
        }
        turns.append(turn)
        try:
            async for frame in real_run_tts(text, context_id):
                if isinstance(frame, ErrorFrame):
                    turn["sent_ok"] = False
                yield frame
        except Exception:
            turn["sent_ok"] = False
            raise

    tts.run_tts = _run_tts
    return turns


class _TurnObserver(BaseObserver):
    """Timestamp requests into ``tts`` and audio/completion out of it.

    Every timestamp is ``FramePushed.timestamp``, captured by the pipeline
    before the frame is queued. Reading ``time.perf_counter()`` inside the
    callback instead measured when this observer happened to be scheduled, which
    a slow consumer can delay arbitrarily.

    Filtering on the processor keeps each frame counted once; the pipeline
    pushes the same frame between several processor pairs.
    """

    def __init__(self, tts, texts):
        """Watch frames into and out of ``tts`` for the given batch texts."""
        super().__init__()
        self._tts = tts
        self._index_of = {text: i for i, text in enumerate(texts)}
        self.requested_at: dict[int, float] = {}
        self.audio_order: list[str] = []
        self.audio_bytes: dict[str, int] = {}
        self.stopped_at: dict[str, float] = {}
        self.stops: list[str] = []
        self.errors: list[str] = []

    async def on_push_frame(self, data: FramePushed):
        """Record request arrival, per-context audio, completion and errors."""
        frame = data.frame
        at = data.timestamp / 1e9
        if data.destination is self._tts:
            if isinstance(frame, TTSSpeakFrame):
                index = self._index_of.get(frame.text.strip())
                if index is not None:
                    self.requested_at[index] = at
            return
        if data.source is not self._tts:
            return
        if isinstance(frame, TTSAudioRawFrame) and frame.audio:
            ctx = frame.context_id or ""
            if ctx not in self.audio_bytes:
                self.audio_order.append(ctx)
            self.audio_bytes[ctx] = self.audio_bytes.get(ctx, 0) + len(frame.audio)
        elif isinstance(frame, TTSStoppedFrame):
            ctx = frame.context_id or ""
            self.stops.append(ctx)
            self.stopped_at.setdefault(ctx, at)
        elif isinstance(frame, ErrorFrame):
            self.errors.append(frame.error)


def _speak_frames(texts, gap: float, observer: _TurnObserver, failures: list[str]):
    """Yield one speak frame per text, waiting for each to complete first.

    ponytail: polls with short sleep frames because ``run_test``'s frame pump
    is a plain loop with no hook to await on. Upgrade path: drive the pipeline
    worker directly if a finer boundary is ever needed.

    A turn that misses the harness deadline ends the batch: the driver stops
    yielding, the pipeline tears down normally, and the failure is asserted
    afterwards. Continuing would have recorded the next turn as an "immediate"
    success measured from a request that overlapped an unfinished one.
    """
    for turn, text in enumerate(texts):
        yield TTSSpeakFrame(text=text)
        waited = 0.0
        while len(observer.stops) <= turn and waited < _UTTERANCE_DEADLINE:
            yield SleepFrame(sleep=_POLL_INTERVAL)
            waited += _POLL_INTERVAL
        if len(observer.stops) <= turn:
            failures.append(
                f"turn {turn} produced no downstream completion within "
                f"{_UTTERANCE_DEADLINE}s; batch abandoned"
            )
            return
        if gap:
            yield SleepFrame(sleep=gap)


_SETUP_EVENTS = ("connect_start", "connect_done", "open", "send:init", "recv:ready")
_TERMINAL_EVENTS = ("recv:audio_end", "recv:flushed")


def _spans(turns, observer: _TurnObserver, log: _WireLog, texts) -> list[dict]:
    """Derive per-utterance spans from explicitly associated events.

    Each turn is anchored on its own recorded ``run_tts`` attempt, so a missing
    or failed send invalidates that sample instead of shifting the next one's
    text, audio and terminal onto it. A span that cannot be observed stays
    ``None`` rather than becoming a fabricated zero, and a sample missing any of
    its send, audio, terminal or downstream completion is not a timing
    observation at all.

    ``prior_wait`` is kept apart from the setup spans: it is the gap between the
    previous utterance's last audio on the wire and this request, so time spent
    waiting for the preceding utterance is never read as preparation for this
    one. ``actual_gap`` is the real spacing achieved between completions, which
    a configured gap only approximates.
    """
    by_index = {}
    for turn in turns:
        index = turn["index"]
        if index is None:
            continue
        # A repeated index means the text no longer identifies its turn.
        by_index.setdefault(index, []).append(turn)

    rows: list[dict] = []
    attempts = sorted(
        (t["attempt_at"], t["index"]) for t in turns if t["index"] is not None
    )
    for index in range(len(texts)):
        request_at = observer.requested_at.get(index)
        row: dict = {
            "index": index,
            "context_id": None,
            "valid": False,
            "invalid_reason": None,
            "request_to_text": None,
            "text_to_audio": None,
            "request_to_audio": None,
            "terminal_to_stop": None,
            "prior_wait": None,
            "actual_gap": None,
            "stop_at": None,
            "received_bytes": None,
            "emitted_bytes": None,
            "foreground_setup": [],
            "terminal": None,
        }
        rows.append(row)

        matches = by_index.get(index, [])
        if request_at is None:
            row["invalid_reason"] = "no request observed"
            continue
        if len(matches) != 1:
            row["invalid_reason"] = f"{len(matches)} sends claimed this text"
            continue
        turn = matches[0]
        row["context_id"] = turn["context_id"]

        attempt_at = turn["attempt_at"]
        window_end = next((at for at, _ in attempts if at > attempt_at), float("inf"))
        prior_audio_at = log.last("audio", request_at) if index else None
        if prior_audio_at is not None:
            row["prior_wait"] = request_at - prior_audio_at
        prior_stop = rows[index - 1]["stop_at"] if index else None
        if prior_stop is not None:
            row["actual_gap"] = request_at - prior_stop

        if not turn["sent_ok"]:
            row["invalid_reason"] = "send failed or errored"
            continue

        text_at = log.first("send:text", attempt_at, window_end)
        if text_at is None:
            row["invalid_reason"] = "no text reached the wire"
            continue
        row["request_to_text"] = text_at - request_at
        row["foreground_setup"] = [
            name for name in log.names(request_at, text_at) if name in _SETUP_EVENTS
        ]

        audio_at = log.first("audio", text_at, window_end)
        terminal_at, terminal = log.first_of(_TERMINAL_EVENTS, text_at, window_end)
        stop_at = observer.stopped_at.get(turn["context_id"])
        row["terminal"] = terminal
        row["received_bytes"] = log.audio_bytes(text_at, window_end)
        row["emitted_bytes"] = observer.audio_bytes.get(turn["context_id"], 0)
        row["stop_at"] = stop_at

        if audio_at is not None:
            row["text_to_audio"] = audio_at - text_at
            row["request_to_audio"] = audio_at - request_at
        if terminal_at is not None and stop_at is not None:
            row["terminal_to_stop"] = stop_at - terminal_at

        if audio_at is None:
            row["invalid_reason"] = "no audio for this turn"
        elif terminal_at is None:
            row["invalid_reason"] = "no terminal message"
        elif stop_at is None:
            row["invalid_reason"] = "no downstream completion"
        elif row["received_bytes"] != row["emitted_bytes"]:
            row["invalid_reason"] = (
                f"received {row['received_bytes']} bytes but emitted "
                f"{row['emitted_bytes']} for this context"
            )
        else:
            row["valid"] = True
    return rows


def _median(values) -> float | None:
    """Median of the observed values, or None when nothing was observed."""
    observed = [v for v in values if v is not None]
    return statistics.median(observed) if observed else None


def _tail(values) -> float | None:
    """Slowest observed value, or None when nothing was observed.

    The worst of a handful of samples, reported as the observed maximum. It is
    not a population percentile and the specification forbids presenting it as
    one.
    """
    observed = [v for v in values if v is not None]
    return max(observed) if observed else None


def _fmt(value) -> str:
    """Render a span in milliseconds, or ``n/a`` when unobserved."""
    return "n/a" if value is None else f"{value * 1000:.0f}ms"


async def _measure_batch(name: str, gap: float, samples: int) -> dict:
    """Run one baseline batch and return its recorded spans and outcomes."""
    texts = list(_MEASURE_TEXTS[:samples])
    assert len(set(texts)) == samples, "batch texts must stay distinct"

    tts = SlngTTSService(
        api_key=os.environ["SLNG_API_KEY"],
        model=os.environ["SLNG_TTS_MODEL"],
        voice=os.environ["SLNG_TTS_VOICE"],
        sample_rate=24000,
        language=_measure_language(),
    )

    failures: list[str] = []
    observer = _TurnObserver(tts, texts)

    # Scoped per batch: a shared monkeypatch would wrap the previous batch's
    # wrapper and count every socket twice.
    with pytest.MonkeyPatch.context() as mp:
        log = _install_wire_log(mp, tts)
        turns = _install_turn_log(tts, log, texts)
        await run_test(
            tts,
            frames_to_send=_speak_frames(texts, gap, observer, failures),
            observers=[observer],
            start_timeout=_START_TIMEOUT,
        )

    connect_start = log.first("connect_start", 0.0)
    connect_done = log.first("open", 0.0)
    init_at = log.first("send:init", 0.0)
    ready_at = log.first("recv:ready", 0.0)
    unclosed = [s for s in log.sockets if s.closed_state is not State.CLOSED]

    return {
        "batch": name,
        "gap": gap,
        "samples": samples,
        "rows": _spans(turns, observer, log, texts),
        "connect": None
        if connect_start is None or connect_done is None
        else connect_done - connect_start,
        "init_to_ready": None
        if init_at is None or ready_at is None
        else ready_at - init_at,
        "startup_to_ready": None
        if connect_start is None or ready_at is None
        else ready_at - connect_start,
        "sockets": len(log.sockets),
        "peak_owned": log.peak_owned,
        "unclosed": len(unclosed),
        "stops": len(observer.stops),
        "errors": list(observer.errors),
        "failures": failures,
        "audio_order": list(observer.audio_order),
        "owners": (tts._websocket, tts._receive_task, tts._keepalive_task),
    }


def _report_group(label: str, rows) -> None:
    """Print medians and observed maxima for one group of samples."""
    valid = [r for r in rows if r["valid"]]
    print(f"  {label}: {len(valid)}/{len(rows)} valid")
    for span in (
        "request_to_text",
        "text_to_audio",
        "request_to_audio",
        "terminal_to_stop",
        "prior_wait",
        "actual_gap",
    ):
        values = [r[span] for r in valid]
        if not any(v is not None for v in values):
            continue
        print(
            f"    {span}: median {_fmt(_median(values))}, "
            f"observed max {_fmt(_tail(values))}, "
            f"n={sum(1 for v in values if v is not None)}"
        )


@measurement
async def test_live_tts_warm_standby_measurement():
    """Measure the existing preparation baseline on an explicitly named route.

    Baseline only: this build has no standby option, so the batches here answer
    one question — after startup connection, keepalive and eager reconnection,
    is there still session setup sitting in front of an utterance's text? Each
    batch is repeated so a later comparison can be judged against baseline
    variation rather than against a single run.

    Asserts the outcomes a consumer depends on (every sample accounted for,
    correctly attributed audio, one completion per turn, no service errors and
    no owned or unclosed connections after teardown) and reports the timings and
    terminal events as evidence. A small ``request_to_text`` is reported as what
    it is — the adapter's own handover — and is not evidence about provider-side
    preparation, which this harness cannot see.
    """
    results = []
    for repeat in range(_MEASURE_REPEATS):
        for name, gap, samples in _MEASURE_BATCHES:
            result = await _measure_batch(name, gap, samples)
            result["repeat"] = repeat
            results.append(result)

    language = os.getenv("SLNG_TTS_LANGUAGE") or "service default"
    print(f"\n=== TTS baseline measurement: {os.environ['SLNG_TTS_MODEL']} ===")
    print(
        f"voice={os.environ['SLNG_TTS_VOICE']} language={language} "
        f"format=linear16/24000/mono credentials=SLNG-managed platform key"
    )
    print(
        f"pipecat {importlib.metadata.version('pipecat-ai')}, {pipecat_slng.__file__}"
    )
    print("gateway runtime/revision and cache state: unknown from the client side")
    for result in results:
        rows = result["rows"]
        print(
            f"\n[{result['batch']} r{result['repeat']}] "
            f"gap={result['gap']}s n={len(rows)} "
            f"valid={sum(1 for r in rows if r['valid'])} "
            f"sockets={result['sockets']} peak_owned={result['peak_owned']} "
            f"unclosed={result['unclosed']} "
            f"completions={result['stops']}/{result['samples']} "
            f"errors={len(result['errors'])}\n"
            f"  connect={_fmt(result['connect'])} "
            f"init->ready={_fmt(result['init_to_ready'])} "
            f"connect->ready={_fmt(result['startup_to_ready'])}"
        )
        _report_group("startup utterance", rows[:1])
        _report_group("subsequent utterances", rows[1:])
        for row in rows:
            print(
                f"  #{row['index']} ctx={row['context_id']} "
                f"valid={row['valid']}{'' if row['valid'] else ' (' + str(row['invalid_reason']) + ')'} "
                f"request->text {_fmt(row['request_to_text'])} "
                f"text->audio {_fmt(row['text_to_audio'])} "
                f"request->audio {_fmt(row['request_to_audio'])} "
                f"terminal->completion {_fmt(row['terminal_to_stop'])} "
                f"prior_wait {_fmt(row['prior_wait'])} "
                f"actual_gap {_fmt(row['actual_gap'])} "
                f"bytes={row['received_bytes']}/{row['emitted_bytes']} "
                f"setup={row['foreground_setup'] or 'none'} "
                f"terminal={row['terminal'] or 'none'}"
            )
        for note in result["failures"] + result["errors"]:
            print(f"  NOTE: {note}")

    print("\n=== repeat variation (subsequent utterances, request->audio) ===")
    for name, _gap, _samples in _MEASURE_BATCHES:
        medians = [
            _median([r["request_to_audio"] for r in res["rows"][1:] if r["valid"]])
            for res in results
            if res["batch"] == name
        ]
        print(f"  {name}: " + ", ".join(_fmt(m) for m in medians))

    for result in results:
        label = f"{result['batch']} r{result['repeat']}"
        assert not result["failures"], f"{label}: {result['failures']}"
        assert not result["errors"], f"{label}: service errors {result['errors']}"
        invalid = [
            f"#{r['index']}: {r['invalid_reason']}"
            for r in result["rows"]
            if not r["valid"]
        ]
        assert not invalid, f"{label}: unusable samples {invalid}"
        assert result["stops"] == result["samples"], (
            f"{label}: {result['stops']} completions for {result['samples']} requests"
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
        assert result["peak_owned"] <= 1, (
            f"{label}: baseline owned {result['peak_owned']} simultaneous sockets"
        )
        assert result["unclosed"] == 0, (
            f"{label}: {result['unclosed']} sockets still open after teardown"
        )
        assert result["owners"] == (None, None, None), (
            f"{label}: resources still owned after teardown: {result['owners']}"
        )


# ---------------------------------------------------------------------------
# Sarvam TTS + STT compatibility smoke (spec 002-fix-tts-completion, US2)
#
# Separately gated on SLNG_SARVAM_SMOKE=1: an ordinary live smoke run must not
# synthesise and re-transcribe a sentence against two Sarvam routes.
# ---------------------------------------------------------------------------

_SARVAM_TTS_MODEL = "sarvam/bulbul:v3"
_SARVAM_TTS_VOICE = "shubh"
_SARVAM_STT_MODEL = "sarvam/saaras:v3"
_SARVAM_RATE = 16000
_SARVAM_SENTENCE = "Please confirm my appointment tomorrow at ten in the morning."
# The transcript comes back with the numeral; nothing else about the sentence is
# allowed to drift.
_SARVAM_EXPECTED = "please confirm my appointment tomorrow at 10 in the morning"

sarvam = pytest.mark.skipif(
    os.getenv("SLNG_SARVAM_SMOKE") != "1",
    reason="needs SLNG_SARVAM_SMOKE=1",
)


def _normalise(text: str) -> str:
    """Lower-case, strip punctuation and spell ten as its numeral."""
    kept = "".join(c for c in text.lower() if c.isalnum() or c.isspace())
    return " ".join(kept.replace(" ten ", " 10 ").split())


@sarvam
async def test_live_sarvam_speech():
    """Synthesise a known sentence on Sarvam, then transcribe that real audio.

    Two stages, reported independently. Sarvam TTS reproduced the completion
    defect this feature fixes — real audio, no downstream stop — so the audio
    and the completion are asserted separately, and the audio is fed to STT even
    if the completion assertion is going to fail. Silence or an empty transcript
    cannot pass: without usable speech there is nothing to transcribe and STT is
    reported as not run.
    """
    tts = SlngTTSService(
        api_key=os.environ["SLNG_API_KEY"],
        model=_SARVAM_TTS_MODEL,
        voice=_SARVAM_TTS_VOICE,
        language=Language.EN_IN,
        sample_rate=_SARVAM_RATE,
    )

    down, _ = await run_test(
        tts,
        frames_to_send=[TTSSpeakFrame(text=_SARVAM_SENTENCE), SleepFrame(sleep=8.0)],
    )

    speech = b"".join(
        f.audio for f in down if isinstance(f, TTSAudioRawFrame) and f.audio
    )
    stops = [f for f in down if isinstance(f, TTSStoppedFrame)]
    tts_errors = [f.error for f in down if isinstance(f, ErrorFrame)]
    tts_owners = (tts._websocket, tts._receive_task, tts._keepalive_task)

    transcript = None
    stt_errors: list[str] = []
    stt_owners = None
    if speech:
        stt = SlngSTTService(
            api_key=os.environ["SLNG_API_KEY"],
            model=_SARVAM_STT_MODEL,
            sample_rate=_SARVAM_RATE,
        )
        # 100ms chunks at 16kHz mono 16-bit, paced like a real caller, then the
        # existing VAD-stop path to finalise. No resampling: the TTS output is
        # already at the rate STT is configured for.
        chunk = _SARVAM_RATE // 10 * 2
        frames: list = [VADUserStartedSpeakingFrame()]
        for start in range(0, len(speech), chunk):
            frames.append(
                InputAudioRawFrame(
                    audio=speech[start : start + chunk],
                    sample_rate=_SARVAM_RATE,
                    num_channels=1,
                )
            )
            frames.append(SleepFrame(sleep=0.05))
        frames += [VADUserStoppedSpeakingFrame(), SleepFrame(sleep=8.0)]

        stt_down, _ = await run_test(stt, frames_to_send=frames)
        finals = [
            f.text
            for f in stt_down
            if isinstance(f, TranscriptionFrame) and f.text.strip()
        ]
        transcript = finals[-1] if finals else None
        stt_errors = [f.error for f in stt_down if isinstance(f, ErrorFrame)]
        stt_owners = (stt._websocket, stt._receive_task)

    print(
        f"\n=== Sarvam speech smoke ===\nTTS {_SARVAM_TTS_MODEL} ({_SARVAM_TTS_VOICE}, en-IN)"
    )
    print(
        f"  audio={len(speech)} bytes stops={len(stops)} errors={tts_errors or 'none'}"
    )
    print(f"STT {_SARVAM_STT_MODEL} (autodetected language, not pinned)")
    print(
        f"  transcript={transcript!r} errors={stt_errors or 'none'}"
        if speech
        else "  not run: TTS returned no usable speech"
    )

    assert speech, "Sarvam TTS returned no audio; STT not run"
    assert not tts_errors, f"Sarvam TTS service errors: {tts_errors}"
    assert len(stops) == 1, (
        f"expected exactly one TTS completion, saw {len(stops)} "
        "(zero is the pre-fix defect this feature corrects)"
    )
    assert stops[0].context_id, "completion carries no synthesis context"
    assert not stt_errors, f"Sarvam STT service errors: {stt_errors}"
    assert transcript, "Sarvam STT returned no final transcript for real speech"
    assert _normalise(transcript) == _SARVAM_EXPECTED, (
        f"transcript {transcript!r} does not match the spoken sentence"
    )
    assert tts_owners == (None, None, None), f"TTS resources still owned: {tts_owners}"
    assert stt_owners == (None, None), f"STT resources still owned: {stt_owners}"
