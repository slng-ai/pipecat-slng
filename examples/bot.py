#
# Copyright (c) 2026, slng.ai
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""SLNG Voice Agent example.

Cascade pipeline: Speech-to-Text → LLM → Text-to-Speech, with SLNG as the
unified STT and TTS gateway.

Required services:
- SLNG (STT + TTS) — set SLNG_API_KEY
- OpenAI (LLM)      — set OPENAI_API_KEY

Run with::

    uv run --extra example examples/bot.py
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
	"""Wire up and run the SLNG cascade pipeline."""
	logger.info("Starting bot")

	stt = SlngSTTService(
		api_key=os.getenv("SLNG_API_KEY"),
		model="slng/deepgram/nova:3-en",
	)

	tts = SlngTTSService(
		api_key=os.getenv("SLNG_API_KEY"),
		model="slng/deepgram/aura:2-en",
		voice="aura-2-thalia-en",
	)

	llm = OpenAIResponsesLLMService(
		api_key=os.getenv("OPENAI_API_KEY"),
		settings=OpenAIResponsesLLMService.Settings(
			model=os.getenv("OPENAI_MODEL", "gpt-4.1"),
			system_instruction=(
				"You are a helpful assistant in a voice conversation. "
				"Your responses will be spoken aloud, so avoid emojis, bullet "
				"points, or other formatting that can't be spoken. Respond to "
				"what the user said in a creative, helpful, and brief way."
			),
		),
	)

	context = LLMContext()
	user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
		context,
		user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer()),
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
		params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
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
	"""Entry point dispatched by the pipecat runner."""
	transport = None
	match runner_args:
		case DailyRunnerArguments():
			# Imported lazily so the example runs with only the transport
			# extra you have installed (Daily needs `pipecat-ai[daily]`).
			from pipecat.transports.daily.transport import DailyParams, DailyTransport

			transport = DailyTransport(
				runner_args.room_url,
				runner_args.token,
				"Pipecat Bot",
				params=DailyParams(audio_in_enabled=True, audio_out_enabled=True),
			)
		case SmallWebRTCRunnerArguments():
			from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport

			transport = SmallWebRTCTransport(
				webrtc_connection=runner_args.webrtc_connection,
				params=TransportParams(audio_in_enabled=True, audio_out_enabled=True),
			)
		case _:
			logger.error(f"Unsupported runner arguments type: {type(runner_args)}")
			return

	await run_bot(transport)


if __name__ == "__main__":
	from pipecat.runner.run import main

	main()
