#
# Copyright (c) 2026, slng.ai
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Live smoke tests against wss://{SLNG_WORLD_PART}.api.slng.ai.

Skipped unless SLNG_API_KEY is set. These hit the real bridge, so they are
excluded from offline/CI-without-secrets runs.
"""

import hashlib
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
import pipecat_slng.stt
import pipecat_slng.tts
from pipecat_slng import SlngSTTService, SlngTTSService

# Region to test against; the default slng/deepgram models run in us-east.
WORLD_PART = os.getenv("SLNG_WORLD_PART", "us-east")

# Marked, not name-matched: `-k 'not live'` also deselects anything whose name
# merely contains "live" — it silently dropped all three keepaLIVE tests.
pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.getenv("SLNG_API_KEY"), reason="SLNG_API_KEY not set"),
]


async def test_live_tts_returns_audio():
    """Real TTS bridge returns audio for a short utterance."""
    tts = SlngTTSService(
        world_part=WORLD_PART,
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
        world_part=WORLD_PART,
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
        world_part=WORLD_PART,
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
        world_part=WORLD_PART,
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


# Route 3: external route WITHOUT a provider key — proxied via SLNG's own
# provider account, billed by SLNG (V21). Needs only SLNG_API_KEY (module mark).
_EXTERNAL_TTS_MODEL = "deepgram/aura:2"
_EXTERNAL_STT_MODEL = "deepgram/nova:3"


async def test_live_route3_external_tts_returns_audio():
    """Route 3 (WS TTS): external route, no provider_key, served by SLNG's account."""
    tts = SlngTTSService(
        world_part=WORLD_PART,
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
        world_part=WORLD_PART,
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
        self._next_socket = 0
        self._audio_hashes: dict = {}
        self.audio_hashes: list[tuple[float, int, str]] = []
        self.selections: dict[str, dict] = {}

    def now(self) -> float:
        """Seconds on the pipeline clock."""
        return self._tts.get_clock().get_time() / 1e9

    def mark(
        self, socket_no: int, label: str, nbytes: int = 0, audio: bytes = b""
    ) -> float:
        """Append one timestamped event and return its timestamp."""
        at = self.now()
        self.events.append((at, socket_no, label, nbytes))
        if label == "send:text":
            self._audio_hashes[socket_no] = hashlib.sha256()
        elif label == "audio":
            digest = self._audio_hashes.setdefault(socket_no, hashlib.sha256())
            digest.update(audio)
            self.audio_hashes.append((at, socket_no, digest.hexdigest()))
        return at

    def reserve(self) -> int:
        """Own a socket from before the connect attempt, and number it."""
        for socket in self.sockets:
            socket.verify_closed()
        self._next_socket += 1
        socket_no = self._next_socket
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
        self.verify_closed()
        return self._inner.state

    def verify_closed(self):
        """Release exactly once, only after observing real transport closure."""
        if not self._closed and self._inner.state is State.CLOSED:
            self._closed = True
            self._log.closed(self._no)

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
            self.verify_closed()

    async def __aiter__(self):
        """Yield received frames, timestamping each one by kind and size."""
        try:
            async for message in self._inner:
                self._log.mark(
                    self._no,
                    _wire_kind(message),
                    len(message) if isinstance(message, bytes) else 0,
                    message if isinstance(message, bytes) else b"",
                )
                yield message
        finally:
            self._log.mark(self._no, "recv_end")
            self.verify_closed()


def _install_wire_log(mp, tts) -> _WireLog:
    """Wrap the TTS module's connector so every socket is timestamped."""
    log = _WireLog(tts)

    async def _connect(url, **kwargs):
        socket_no = log.reserve()
        try:
            inner = await _REAL_WS_CONNECT(url, **kwargs)
        except BaseException:
            log.failed_open(socket_no)
            raise
        socket = _TimedSocket(inner, log, socket_no)
        log.sockets.append(socket)
        log.mark(socket_no, "open")
        return socket

    mp.setattr("pipecat_slng.tts.websocket_connect", _connect)
    return log


def _install_turn_log(tts, log: _WireLog, texts, observer) -> list[dict]:
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
    real_completed = tts.on_audio_context_completed

    async def _completed(context_id):
        await real_completed(context_id)
        observer.completed_at[context_id] = log.now()

    tts.on_audio_context_completed = _completed
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
        self.audio_hashes: dict = {}
        self.audio_times: dict[str, list[float]] = {}
        self.completed_at: dict[str, float] = {}
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
            if not self.audio_order or self.audio_order[-1] != ctx:
                self.audio_order.append(ctx)
            self.audio_times.setdefault(ctx, []).append(at)
            self.audio_hashes.setdefault(ctx, hashlib.sha256()).update(frame.audio)
            self.audio_bytes[ctx] = self.audio_bytes.get(ctx, 0) + len(frame.audio)
        elif isinstance(frame, TTSStoppedFrame):
            ctx = frame.context_id or ""
            self.stops.append(ctx)
            self.stopped_at.setdefault(ctx, at)
        elif isinstance(frame, ErrorFrame):
            self.errors.append(frame.error)


def _speak_frames(
    texts, gap: float, observer: _TurnObserver, failures: list[str], turns
):
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
        while waited < _UTTERANCE_DEADLINE:
            matches = [t for t in turns if t["index"] == turn]
            ctx = matches[0]["context_id"] if len(matches) == 1 else None
            if observer.errors:
                failures.append(f"turn {turn}: service error; batch abandoned")
                return
            if ctx and ctx in observer.stopped_at and ctx in observer.completed_at:
                break
            yield SleepFrame(sleep=_POLL_INTERVAL)
            waited += _POLL_INTERVAL
        else:
            failures.append(
                f"turn {turn} produced no matching downstream/context completion within "
                f"{_UTTERANCE_DEADLINE}s; batch abandoned"
            )
            return
        if gap and turn < len(texts) - 1:
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
        # Only sockets carrying this turn's text can supply its audio or setup.
        # An idle spare's ready/error/audio is never foreground work.
        sockets = {
            no
            for at, no, name, _ in log.events
            if name == "send:text" and attempt_at <= at < window_end
        }
        events = [
            (at, name, size) for at, no, name, size in log.events if no in sockets
        ]
        audio_events = [
            (at, size)
            for at, name, size in events
            if name == "audio" and text_at <= at < window_end
        ]
        row["request_to_text"] = text_at - request_at
        row["foreground_setup"] = [
            name
            for at, name, _ in events
            if request_at <= at < text_at and name in _SETUP_EVENTS
        ]
        selection = log.selections.get(turn["context_id"])
        row["standby"] = selection
        if selection and selection["used"]:
            ready = [
                (at, no)
                for at, no, name, _ in log.events
                if no in sockets and name == "recv:ready" and at <= selection["at"]
            ]
            if len(sockets) != 1 or not ready or row["foreground_setup"]:
                row["invalid_reason"] = (
                    "standby hit lacks prior readiness on speech socket"
                )
                continue
        audio_at = audio_events[0][0] if audio_events else None
        terminal_at, terminal = next(
            (
                (at, name)
                for at, name, _ in events
                if name in _TERMINAL_EVENTS and text_at <= at < window_end
            ),
            (None, None),
        )
        stop_at = observer.stopped_at.get(turn["context_id"])
        row["terminal"] = terminal
        row["received_bytes"] = sum(size for _, size in audio_events)
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
        elif not (
            request_at <= attempt_at <= text_at <= audio_at <= terminal_at <= stop_at
            and audio_at
            <= min(observer.audio_times.get(turn["context_id"], [float("inf")]))
            and max(observer.audio_times.get(turn["context_id"], [float("inf")]))
            <= stop_at
            and audio_events[-1][0] <= terminal_at
        ):
            row["invalid_reason"] = "invalid event ordering"
        elif stop_at > observer.completed_at.get(turn["context_id"], float("-inf")):
            row["invalid_reason"] = "no matching context completion"
        elif observer.stops.count(turn["context_id"]) != 1:
            row["invalid_reason"] = "duplicate completion"
        elif row["received_bytes"] != row["emitted_bytes"]:
            row["invalid_reason"] = (
                f"received {row['received_bytes']} bytes but emitted "
                f"{row['emitted_bytes']} for this context"
            )
        elif (
            next(
                (
                    digest
                    for at, no, digest in reversed(log.audio_hashes)
                    if no in sockets and text_at <= at < window_end
                ),
                None,
            )
            != observer.audio_hashes[turn["context_id"]].hexdigest()
        ):
            row["invalid_reason"] = "audio content/order mismatch"
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


async def _measure_batch(
    name: str,
    gap: float,
    samples: int,
    *,
    warm_standby_enabled: bool = False,
    force_reconnect: bool = False,
) -> dict:
    """Measure natural behavior or explicitly forced per-turn reconnection."""
    texts = list(_MEASURE_TEXTS[:samples])
    assert len(set(texts)) == samples, "batch texts must stay distinct"

    tts = SlngTTSService(
        world_part=WORLD_PART,
        api_key=os.environ["SLNG_API_KEY"],
        model=os.environ["SLNG_TTS_MODEL"],
        voice=os.environ["SLNG_TTS_VOICE"],
        sample_rate=24000,
        language=_measure_language(),
        warm_standby_enabled=warm_standby_enabled,
    )

    # Test-only controlled condition, never presented as natural route behavior.
    tts._session_dies_per_utterance = force_reconnect
    failures: list[str] = []
    observer = _TurnObserver(tts, texts)

    # Scoped per batch: a shared monkeypatch would wrap the previous batch's
    # wrapper and count every socket twice.
    with pytest.MonkeyPatch.context() as mp:
        log = _install_wire_log(mp, tts)
        turns = _install_turn_log(tts, log, texts, observer)
        from loguru import logger

        def selection(message):
            event = message.record["extra"].get("slng_warm_standby")
            if event is not None:
                log.selections[event["context_id"]] = dict(event, at=log.now())

        sink = logger.add(selection, level="DEBUG")
        try:
            await run_test(
                tts,
                frames_to_send=_speak_frames(texts, gap, observer, failures, turns),
                observers=[observer],
                start_timeout=_START_TIMEOUT,
            )
        finally:
            logger.remove(sink)

    connect_start = log.first("connect_start", 0.0)
    connect_done = log.first("open", 0.0)
    init_at = log.first("send:init", 0.0)
    ready_at = log.first("recv:ready", 0.0)
    unclosed = [s for s in log.sockets if s.closed_state is not State.CLOSED]

    return {
        "batch": name,
        "warm_standby_enabled": warm_standby_enabled,
        "forced_reconnect": force_reconnect,
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
        "owned": log._owned,
        "stops": len(observer.stops),
        "errors": list(observer.errors),
        "failures": failures,
        "audio_order": list(observer.audio_order),
        "owners": (
            tts._websocket,
            tts._receive_task,
            tts._keepalive_task,
            tts._standby_ws,
            tts._standby_claimed,
            tts._standby_task,
            tts._standby_retiring,
            tts._standby_close_task,
        ),
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


def _qualify_batch(result):
    """Reject unusable observations after the pipeline has cleaned up."""
    label = f"{result['batch']} r{result['repeat']}"
    assert not result["failures"], f"{label}: {result['failures']}"
    assert not result["errors"], f"{label}: service errors {result['errors']}"
    invalid = [
        f"#{r['index']}: {r['invalid_reason']}"
        for r in result["rows"]
        if not r["valid"]
    ]
    assert not invalid, f"{label}: unusable samples {invalid}"
    if result.get("warm_standby_enabled"):
        assert all(
            (row.get("standby") or {}).get("enabled") for row in result["rows"]
        ), f"{label}: missing actual standby selection"
    assert result["stops"] == result["samples"], (
        f"{label}: {result['stops']} completions for {result['samples']} requests"
    )
    # Audio for one context must finish before the next context's begins;
    # a context reappearing later means interleaved or stale playback.
    seen: list[str] = []
    for context_id in result["audio_order"]:
        if not seen or seen[-1] != context_id:
            assert context_id not in seen, f"{label}: audio for {context_id} resumed"
            seen.append(context_id)
    assert seen == [row["context_id"] for row in result["rows"]], (
        f"{label}: audio contexts are out of request order"
    )
    limit = 2 if result.get("warm_standby_enabled") else 1
    assert result["peak_owned"] <= limit, (
        f"{label}: owned {result['peak_owned']} simultaneous sockets (limit {limit})"
    )
    assert result["owned"] == 0, f"{label}: socket reservations remain owned"
    assert result["unclosed"] == 0, (
        f"{label}: {result['unclosed']} sockets still open after teardown"
    )
    assert all(owner is None for owner in result["owners"]), (
        f"{label}: resources still owned after teardown: {result['owners']}"
    )


@measurement
async def test_live_tts_warm_standby_measurement():
    """Measure the existing preparation baseline on an explicitly named route.

    Set SLNG_TTS_STANDBY_COMPARE=1 for interleaved baseline/standby/baseline
    batches. SLNG_TTS_FORCE_RECONNECT=1 separately models a route that requires
    a fresh session each turn; those results are labelled as controlled rather
    than natural provider behavior. Defaults preserve the existing baseline run.

    Asserts the outcomes a consumer depends on (every sample accounted for,
    correctly attributed audio, one completion per turn, no service errors and
    no owned or unclosed connections after teardown) and reports the timings and
    terminal events as evidence. A small ``request_to_text`` is reported as what
    it is — the adapter's own handover — and is not evidence about provider-side
    preparation, which this harness cannot see.
    """
    results = []
    modes = (
        (False, True, False)
        if os.getenv("SLNG_TTS_STANDBY_COMPARE") == "1"
        else (False,)
    )
    for repeat in range(_MEASURE_REPEATS):
        for name, gap, samples in _MEASURE_BATCHES:
            for enabled in modes:
                result = await _measure_batch(
                    name,
                    gap,
                    samples,
                    warm_standby_enabled=enabled,
                    force_reconnect=os.getenv("SLNG_TTS_FORCE_RECONNECT") == "1",
                )
                result["repeat"] = repeat
                results.append(result)
                print(
                    "BATCH_RESULT "
                    + json.dumps({k: v for k, v in result.items() if k != "owners"})
                )
                _qualify_batch(result)

    language = os.getenv("SLNG_TTS_LANGUAGE") or "service default"
    print(f"\n=== TTS measurement: {os.environ['SLNG_TTS_MODEL']} ===")
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
            f"standby={result['warm_standby_enabled']} forced_reconnect={result['forced_reconnect']} "
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
            if res["batch"] == name and not res["warm_standby_enabled"]
        ]
        print(f"  {name}: " + ", ".join(_fmt(m) for m in medians))


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
        world_part=WORLD_PART,
        api_key=os.environ["SLNG_API_KEY"],
        model=_SARVAM_TTS_MODEL,
        voice=_SARVAM_TTS_VOICE,
        language=Language.EN_IN,
        sample_rate=_SARVAM_RATE,
        warm_standby_enabled=os.getenv("SLNG_TTS_STANDBY_COMPARE") == "1",
    )

    sockets = {"tts": [], "stt": []}

    async def run_leg(service, frames, kind):
        module = pipecat_slng.tts if kind == "tts" else pipecat_slng.stt
        connect = module.websocket_connect

        async def capture(*args, **kwargs):
            ws = await connect(*args, **kwargs)
            sockets[kind].append(ws)
            return ws

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(module, "websocket_connect", capture)
            return await run_test(
                service, frames_to_send=frames, start_timeout=_START_TIMEOUT
            )

    down, up = await run_leg(
        tts, [TTSSpeakFrame(text=_SARVAM_SENTENCE), SleepFrame(sleep=8.0)], "tts"
    )

    speech = b"".join(
        f.audio for f in down if isinstance(f, TTSAudioRawFrame) and f.audio
    )
    stops = [f for f in down if isinstance(f, TTSStoppedFrame)]
    tts_errors = [f.error for f in [*down, *up] if isinstance(f, ErrorFrame)]
    tts_owners = (tts._websocket, tts._receive_task, tts._keepalive_task)

    transcript = None
    stt_errors: list[str] = []
    stt_owners = None
    if speech:
        stt = SlngSTTService(
            world_part=WORLD_PART,
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

        stt_down, stt_up = await run_leg(stt, frames, "stt")
        finals = [
            f.text
            for f in stt_down
            if isinstance(f, TranscriptionFrame) and f.text.strip()
        ]
        transcript = finals[-1] if finals else None
        stt_errors = [
            f.error for f in [*stt_down, *stt_up] if isinstance(f, ErrorFrame)
        ]
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
    audio_frames = [f for f in down if isinstance(f, TTSAudioRawFrame) and f.audio]
    assert stops[0].context_id and {f.context_id for f in audio_frames} == {
        stops[0].context_id
    }, "audio/completion context mismatch"
    assert all(down.index(f) < down.index(stops[0]) for f in audio_frames), (
        "audio after completion"
    )
    assert all(
        f.sample_rate == _SARVAM_RATE and f.num_channels == 1 for f in audio_frames
    ), "unexpected speech format"
    assert not stt_errors, f"Sarvam STT service errors: {stt_errors}"
    assert transcript, "Sarvam STT returned no final transcript for real speech"
    assert _normalise(transcript) == _SARVAM_EXPECTED, (
        f"transcript {transcript!r} does not match the spoken sentence"
    )
    assert tts_owners == (None, None, None), f"TTS resources still owned: {tts_owners}"
    assert stt_owners == (None, None), f"STT resources still owned: {stt_owners}"

    for kind, captured in sockets.items():
        assert captured and all(ws.state is State.CLOSED for ws in captured), (
            f"{kind}: sockets not closed"
        )
        print(f"  {kind}: {len(captured)} sockets verified CLOSED")
