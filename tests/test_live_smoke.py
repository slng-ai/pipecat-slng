#
# Copyright (c) 2026, slng.ai
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Live smoke tests against wss://api.slng.ai.

Skipped unless SLNG_API_KEY is set. These hit the real bridge, so they are
excluded from offline/CI-without-secrets runs.
"""

import os

import pytest
from pipecat.frames.frames import (
    InputAudioRawFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSSpeakFrame,
)
from pipecat.tests.utils import SleepFrame, run_test

from pipecat_slng import SlngHttpTTSService, SlngSTTService, SlngTTSService

pytestmark = pytest.mark.skipif(
    not os.getenv("SLNG_API_KEY"), reason="SLNG_API_KEY not set"
)


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
