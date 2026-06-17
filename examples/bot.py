#
# Copyright (c) 2026, slng.ai
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""SLNG Voice Agent example.

Cascade pipeline: Speech-to-Text → LLM → Text-to-Speech, with SLNG as the
unified STT and TTS gateway. Defaults to the streaming WebSocket TTS service
(``SlngTTSService``) for low-latency, interruptible conversation.

Required services:
- SLNG (STT + TTS) — set SLNG_API_KEY
- OpenAI (LLM)      — set OPENAI_API_KEY

Optional:
- Model routing — set SLNG_STT_MODEL / SLNG_TTS_MODEL (+ SLNG_TTS_VOICE) to
  pick routes; both default to SLNG-hosted ``slng/...`` routes.
- BYOK — set SLNG_PROVIDER_KEY to your own provider key. On an external route
  (a model string without the ``slng/`` prefix) the provider bills you
  directly. See https://docs.slng.ai/execution-layer/byok

Run with::

    cp .env.example .env   # set SLNG_API_KEY and OPENAI_API_KEY
    uv run --extra example examples/bot.py

Then open http://localhost:7860/client in your browser and start talking.
Uses the SmallWebRTC transport by default; pass ``-t daily`` to use Daily
instead (requires ``pipecat-ai[daily]``).
"""

import os

from dotenv import load_dotenv
from loguru import logger

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.runner.types import (
    DailyRunnerArguments,
    RunnerArguments,
    SmallWebRTCRunnerArguments,
)
from pipecat.services.openai.responses.llm import OpenAIResponsesLLMService
from pipecat.transports.base_transport import BaseTransport, TransportParams

from pipecat_slng import SlngSTTService, SlngTTSService

load_dotenv(override=True)


async def run_bot(transport: BaseTransport):
    """Main bot logic."""
    logger.info("Starting bot")

    slng_api_key = os.environ["SLNG_API_KEY"]

    # Model routing is independent of BYOK:
    #  - slng/... routes are hosted by SLNG (the defaults below).
    #  - any other route (e.g. deepgram/aura:2) is an external provider proxied
    #    through SLNG; it runs with OR without your own key.
    # BYOK (optional): set SLNG_PROVIDER_KEY to your own provider key and the bot
    # forwards it as X-Slng-Provider-Key, so the provider bills you directly. Only
    # valid on external routes. https://docs.slng.ai/execution-layer/byok
    stt_model = os.getenv("SLNG_STT_MODEL", "slng/deepgram/nova:3-en")
    tts_model = os.getenv("SLNG_TTS_MODEL", "slng/deepgram/aura:2-en")
    tts_voice = os.getenv("SLNG_TTS_VOICE", "aura-2-thalia-en")
    provider_key = os.getenv("SLNG_PROVIDER_KEY")

    stt = SlngSTTService(
        api_key=slng_api_key,
        model=stt_model,
        provider_key=provider_key,
    )

    # Streaming WebSocket TTS — low latency, supports mid-utterance interruption.
    # Swap SLNG_TTS_MODEL to switch route. For non-streaming HTTP, see SlngHttpTTSService.
    tts = SlngTTSService(
        api_key=slng_api_key,
        model=tts_model,
        voice=tts_voice,
        provider_key=provider_key,
    )

    llm = OpenAIResponsesLLMService(
        api_key=os.getenv("OPENAI_API_KEY"),
        settings=OpenAIResponsesLLMService.Settings(
            model=os.getenv("OPENAI_MODEL", "gpt-4.1"),
            system_instruction=(
                "You are a helpful assistant in a voice conversation. "
                "Your responses will be spoken aloud, so avoid emojis, bullet points, "
                "or other formatting that can't be spoken. "
                "Respond to what the user said in a creative, helpful, and brief way."
            ),
        ),
    )

    context = LLMContext()
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(),
        ),
    )

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            user_aggregator,
            llm,
            tts,
            transport.output(),
            assistant_aggregator,
        ]
    )

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
        observers=[],
    )

    @task.rtvi.event_handler("on_client_ready")
    async def on_client_ready(rtvi):
        context.add_message({"role": "user", "content": "Please introduce yourself."})
        await task.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info("Client connected")

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("Client disconnected")
        await task.cancel()

    runner = PipelineRunner(handle_sigint=False)
    await runner.run(task)


async def bot(runner_args: RunnerArguments):
    """Main bot entry point."""
    transport = None

    match runner_args:
        case DailyRunnerArguments():
            # Imported lazily so the bot runs with only the transport extra
            # you have installed (Daily needs `pipecat-ai[daily]`).
            from pipecat.transports.daily.transport import DailyParams, DailyTransport

            transport = DailyTransport(
                runner_args.room_url,
                runner_args.token,
                "PipecatSLNG Bot",
                params=DailyParams(
                    audio_in_enabled=True,
                    audio_out_enabled=True,
                ),
            )
        case SmallWebRTCRunnerArguments():
            from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport

            transport = SmallWebRTCTransport(
                webrtc_connection=runner_args.webrtc_connection,
                params=TransportParams(
                    audio_in_enabled=True,
                    audio_out_enabled=True,
                ),
            )
        case _:
            logger.error(f"Unsupported runner arguments type: {type(runner_args)}")
            return

    await run_bot(transport)


if __name__ == "__main__":
    from pipecat.runner.run import main

    main()
