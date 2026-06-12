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


byok = pytest.mark.skipif(
    not os.getenv("DEEPGRAM_API_KEY"), reason="DEEPGRAM_API_KEY not set"
)


@byok
async def test_live_byok_tts_returns_audio():
    """BYOK: external route + provider_key returns audio, billed upstream."""
    tts = SlngTTSService(
        api_key=os.environ["SLNG_API_KEY"],
        model="deepgram/aura:2",
        voice="aura-2-thalia-en",
        sample_rate=24000,
        provider_key=os.environ["DEEPGRAM_API_KEY"],
    )

    down, _ = await run_test(
        tts,
        frames_to_send=[TTSSpeakFrame(text="Hello from BYOK."), SleepFrame(sleep=3.0)],
    )

    assert any(isinstance(f, TTSAudioRawFrame) and f.audio for f in down)


@byok
async def test_live_byok_stt_connects_and_finalizes():
    """BYOK: external STT route + provider_key accepts audio without erroring."""
    stt = SlngSTTService(
        api_key=os.environ["SLNG_API_KEY"],
        model="deepgram/nova:3",
        sample_rate=16000,
        provider_key=os.environ["DEEPGRAM_API_KEY"],
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
    """BYOK: external HTTP TTS route + provider_key returns audio."""
    tts = SlngHttpTTSService(
        api_key=os.environ["SLNG_API_KEY"],
        model="deepgram/aura:2",
        voice="aura-2-thalia-en",
        sample_rate=24000,
        provider_key=os.environ["DEEPGRAM_API_KEY"],
    )

    down, _ = await run_test(
        tts,
        frames_to_send=[
            TTSSpeakFrame(text="Hello from BYOK over HTTP."),
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
