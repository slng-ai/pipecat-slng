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

from pipecat_slng import SlngHttpTTSService, SlngSTTService, SlngTTSService


# The 13 SLNG destinations from spec.md. Routing uses the prefix itself; the
# descriptive groups (Americas/Europes/Asia) are never hostnames.
_DESTINATIONS = (
    "us-east",
    "us-west",
    "br",
    "eu-west",
    "eu-north",
    "gb",
    "za",
    "il",
    "jp",
    "sg",
    "id",
    "in",
    "au",
)
# A prefix SLNG has not provisioned yet, plus both length boundaries, must work
# without a package update — the adapter validates syntax, not a catalog.
_FUTURE_PREFIXES = ("mars-1", "a", "a" * 63)
_WORLD_PART = "eu-west"

_STT_ROUTE = "/v1/bridges/unmute/stt/"
_DEFAULT_MODEL_PATH = "slng/deepgram/nova:3-en"


def _make_stt():
    return SlngSTTService(api_key="test-key", world_part=_WORLD_PART, sample_rate=16000)


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


@pytest.mark.parametrize("world_part", [*_DESTINATIONS, *_FUTURE_PREFIXES])
async def test_stt_default_host_is_world_part_prefix(patch_ws, world_part):
    """With no base override the host is exactly ``{world_part}.api.slng.ai``.

    Unprefixed ``api.slng.ai`` is reachable only by explicitly passing it as
    ``base_url`` — never as a default or a fallback.
    """
    fake = patch_ws("pipecat_slng.stt", [])
    stt = SlngSTTService(api_key="test-key", world_part=world_part, sample_rate=16000)

    await stt._connect_websocket()

    assert fake.connect_url == (
        f"wss://{world_part}.api.slng.ai{_STT_ROUTE}{_DEFAULT_MODEL_PATH}"
    )
    # The hostname carries the routing choice: no legacy header, no init field.
    assert list(fake.connect_headers) == ["Authorization"]
    init = json.loads(fake.sent[0])
    assert not {"region", "world-part", "world_part"} & (
        set(init) | set(init["config"])
    )


@pytest.mark.parametrize(
    ("base_url", "expected_base"),
    [
        (None, "wss://in.api.slng.ai"),
        ("api.slng.ai", "wss://api.slng.ai"),
        ("wss://api.slng.ai", "wss://api.slng.ai"),
        # Already prefixed hosts are kept as supplied — never re-prefixed,
        # whether or not they match world_part.
        ("gb.api.slng.ai", "wss://gb.api.slng.ai"),
        ("in.api.slng.ai", "wss://in.api.slng.ai"),
        ("wss://GB.API.SLNG.AI.", "wss://GB.API.SLNG.AI."),
        ("ws://staging.example:8080/gateway/", "ws://staging.example:8080/gateway"),
    ],
)
async def test_stt_explicit_base_keeps_supplied_host(patch_ws, base_url, expected_base):
    """An explicit base overrides world-part host generation, unchanged.

    The first two rows are spec.md's two exact ``sarvam/saaras:v3`` URLs.
    """
    fake = patch_ws("pipecat_slng.stt", [])
    stt = SlngSTTService(
        api_key="test-key",
        world_part="in",
        base_url=base_url,
        model="sarvam/saaras:v3",
        sample_rate=16000,
    )

    await stt._connect_websocket()

    assert fake.connect_url == f"{expected_base}{_STT_ROUTE}sarvam/saaras:v3"


@pytest.mark.parametrize(
    ("base_url", "expected_base"),
    [(None, "wss://eu-north.api.slng.ai"), ("api.slng.ai", "wss://api.slng.ai")],
)
async def test_stt_base_retained_on_reconnect_and_model_change(
    patch_ws, base_url, expected_base
):
    """Reconnects keep the selected base; only the escaped model route moves."""
    fake = patch_ws("pipecat_slng.stt", [])
    stt = SlngSTTService(
        api_key="test-key", world_part="eu-north", base_url=base_url, sample_rate=16000
    )

    await stt._connect_websocket()
    first_url = fake.connect_url

    stt._websocket = None  # force the reconnect path
    stt._settings.model = "my provider/model:v1"
    await stt._connect_websocket()

    assert first_url == f"{expected_base}{_STT_ROUTE}{_DEFAULT_MODEL_PATH}"
    assert fake.connect_url == f"{expected_base}{_STT_ROUTE}my%20provider/model:v1"


async def test_stt_instances_route_independently(patch_ws):
    """One service's destination never changes another's."""
    fake = patch_ws("pipecat_slng.stt", [])
    automatic = SlngSTTService(api_key="test-key", world_part="jp", sample_rate=16000)
    explicit = SlngSTTService(
        api_key="test-key", world_part="jp", base_url="api.slng.ai", sample_rate=16000
    )

    await automatic._connect_websocket()
    automatic_url = fake.connect_url
    await explicit._connect_websocket()

    assert automatic_url == f"wss://jp.api.slng.ai{_STT_ROUTE}{_DEFAULT_MODEL_PATH}"
    assert fake.connect_url == f"wss://api.slng.ai{_STT_ROUTE}{_DEFAULT_MODEL_PATH}"


@pytest.mark.parametrize(
    ("base_url", "expected_host"),
    [(None, "za.api.slng.ai"), ("api.slng.ai", "api.slng.ai")],
)
async def test_stt_connect_failure_never_falls_back(
    monkeypatch, base_url, expected_host
):
    """An unavailable destination is retried as itself, never as another host."""
    attempts: list[str] = []

    async def _reject(url, **kwargs):
        attempts.append(url)
        raise OSError("destination unavailable")

    monkeypatch.setattr("pipecat_slng.stt.websocket_connect", _reject)
    stt = SlngSTTService(
        api_key="test-key", world_part="za", base_url=base_url, sample_rate=16000
    )

    async def _swallow(error_msg, exception=None):
        pass

    monkeypatch.setattr(stt, "push_error", _swallow)

    for _ in range(2):
        with pytest.raises(OSError):
            await stt._connect_websocket()

    assert attempts == [f"wss://{expected_host}{_STT_ROUTE}{_DEFAULT_MODEL_PATH}"] * 2


async def test_provider_key_header_sent(patch_ws):
    """provider_key maps to the X-Slng-Provider-Key header (BYOK)."""
    fake = patch_ws("pipecat_slng.stt", [json.dumps({"type": "ready"})])
    stt = SlngSTTService(
        api_key="test-key",
        world_part=_WORLD_PART,
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
        world_part=_WORLD_PART,
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


# ---------------------------------------------------------------------------
# Constructor boundary — shared by all three services (US2)
# ---------------------------------------------------------------------------

_SERVICES = (SlngSTTService, SlngTTSService, SlngHttpTTSService)

_INVALID_WORLD_PARTS = (
    None,
    "",
    " ",
    "\t",
    "GB",  # uppercase is rejected, never silently lowercased
    "eu west",
    "eu_west",
    "-gb",
    "gb-",
    "a" * 64,
    "eu-west.api.slng.ai",  # a full host would double the hostname
    "wss://eu-west.api.slng.ai",
    5,
    b"gb",
)

# Bases that are malformed for every service. Already prefixed SLNG hosts are
# valid overrides instead — see test_stt_explicit_base_keeps_supplied_host.
_INVALID_BASES = (
    "",
    "   ",
    0,
    b"api.slng.ai",
    "api.slng.ai?region=eu",
    "api.slng.ai#eu",
    "api.slng.ai\nevil.example",
    "api.slng.ai:",
    "api.slng.ai:abc",
    "api.slng.ai:99999",
    "api.slng.ai:0",
    "1.2.3.4",
    "[::1]",
    "under_score.example",
    "://api.slng.ai",
)

_REMOVED_OVERRIDES = (
    {"region_override": "eu-north-1"},
    {"world_part_override": "eu"},
    {"region_override": None},  # a null value is still an explicit use
    {"world_part_override": None},
    {"region_override": "eu-north-1", "world_part_override": "eu"},
    {"region_override": None, "world_part_override": None},
)


def _construct(cls, **kwargs):
    """Construct any of the three services with the given routing arguments."""
    return cls(api_key="test-key", **kwargs)


def _legacy_base(cls) -> str:
    """The legacy unprefixed gateway in the form this service accepts."""
    return "https://api.slng.ai" if cls is SlngHttpTTSService else "api.slng.ai"


def _own_scheme(cls, rest: str) -> str:
    """A base with this service's own scheme, so scheme checks pass first."""
    return f"https://{rest}" if cls is SlngHttpTTSService else f"wss://{rest}"


@pytest.mark.parametrize("cls", _SERVICES)
@pytest.mark.parametrize("with_base", [False, True])
async def test_omitted_world_part_raises_native_missing_keyword(cls, with_base):
    """Omitting the destination is the native missing-argument error."""
    extra = {"base_url": _legacy_base(cls)} if with_base else {}
    with pytest.raises(TypeError, match="missing.*world_part"):
        _construct(cls, **extra)


@pytest.mark.parametrize("cls", _SERVICES)
@pytest.mark.parametrize("removed", _REMOVED_OVERRIDES)
async def test_omitted_world_part_outranks_removed_override_error(cls, removed):
    """A missing required keyword is reported before the migration error."""
    with pytest.raises(TypeError, match="missing.*world_part"):
        _construct(cls, **removed)


@pytest.mark.parametrize("cls", _SERVICES)
@pytest.mark.parametrize("world_part", _INVALID_WORLD_PARTS)
@pytest.mark.parametrize("with_base", [False, True])
async def test_invalid_world_part_is_rejected(cls, world_part, with_base):
    """An invalid prefix fails at construction, with or without a base override.

    An explicit base does not consume the destination: it stays required and
    validated so a typo can never quietly fall through to another host.
    """
    extra = {"base_url": _legacy_base(cls)} if with_base else {}
    with pytest.raises(ValueError, match="world_part"):
        _construct(cls, world_part=world_part, **extra)


@pytest.mark.parametrize("cls", _SERVICES)
@pytest.mark.parametrize("removed", _REMOVED_OVERRIDES)
async def test_removed_regional_overrides_are_rejected(cls, removed):
    """Removed settings must not disappear into Pipecat's tolerant **kwargs."""
    with pytest.raises(TypeError) as exc:
        _construct(cls, world_part=_WORLD_PART, **removed)

    message = str(exc.value)
    assert all(name in message for name in removed)
    assert "world_part" in message


@pytest.mark.parametrize("cls", _SERVICES)
@pytest.mark.parametrize("base_url", _INVALID_BASES)
async def test_invalid_explicit_base_is_rejected(cls, base_url):
    """A malformed base is an error, never a silent switch to automatic routing."""
    with pytest.raises(ValueError, match="base_url"):
        _construct(cls, world_part=_WORLD_PART, base_url=base_url)


@pytest.mark.parametrize(
    ("cls", "base_url"),
    [
        (SlngSTTService, "https://api.slng.ai"),
        (SlngTTSService, "http://api.slng.ai"),
        (SlngHttpTTSService, "api.slng.ai"),  # HTTP needs a full URL, not a host
        (SlngHttpTTSService, "wss://api.slng.ai"),
    ],
)
async def test_base_scheme_must_match_the_transport(cls, base_url):
    """Each service accepts only the schemes it can actually connect with."""
    with pytest.raises(ValueError, match="base_url"):
        _construct(cls, world_part=_WORLD_PART, base_url=base_url)


@pytest.mark.parametrize("cls", _SERVICES)
@pytest.mark.parametrize("host", ["staging.example", "staging.example\uff0f", "["])
async def test_base_credentials_rejected_without_echoing_the_secret(cls, host):
    """Guidance names the setting; it never repeats an embedded credential."""
    secret = "s3cr3t-token"
    with pytest.raises(ValueError) as exc:
        _construct(
            cls,
            world_part=_WORLD_PART,
            base_url=_own_scheme(cls, f"user:{secret}@{host}"),
        )

    assert "base_url" in str(exc.value)
    assert secret not in str(exc.value)


async def test_invalid_config_never_reaches_parent_init(monkeypatch):
    """Validation precedes super().__init__, which allocates local resources."""
    from pipecat.processors.frame_processor import FrameProcessor

    initialized: list[str] = []
    original = FrameProcessor.__init__

    def _record(self, *args, **kwargs):
        initialized.append(type(self).__name__)
        original(self, *args, **kwargs)

    monkeypatch.setattr(FrameProcessor, "__init__", _record)

    for cls in _SERVICES:
        for bad in (
            {"world_part": "GB"},
            {"world_part": _WORLD_PART, "region_override": "eu-north-1"},
            {"world_part": _WORLD_PART, "base_url": ""},
        ):
            with pytest.raises((TypeError, ValueError)):
                _construct(cls, **bad)

    assert initialized == []
    # The patch does record a valid construction, so the check above is real.
    _construct(SlngSTTService, world_part=_WORLD_PART)
    assert initialized == ["SlngSTTService"]


@pytest.mark.parametrize("cls", _SERVICES)
async def test_unrelated_pipecat_kwargs_still_pass_through(cls):
    """Rejecting the old names must not reject legitimate parent kwargs."""
    service = _construct(cls, world_part=_WORLD_PART, name="my-service")

    assert service.name == "my-service"
