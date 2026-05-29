#
# Copyright (c) 2026, slng.ai
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Unit tests for SlngTTSService using a fake WebSocket."""

import asyncio
import json

from pipecat.frames.frames import TTSAudioRawFrame, TTSSpeakFrame
from pipecat.tests.utils import SleepFrame, run_test

from pipecat_slng import SlngTTSService


def _make_tts():
	return SlngTTSService(
		api_key="test-key",
		voice="aura-2-thalia-en",
		sample_rate=24000,
	)


async def test_init_message_includes_voice(patch_ws):
	"""Init message carries voice at top level and config fields."""
	fake = patch_ws("pipecat_slng.tts", [json.dumps({"type": "ready"})])
	tts = _make_tts()

	await run_test(tts, frames_to_send=[SleepFrame(sleep=0.1)])

	text_sends = [json.loads(s) for s in fake.sent if isinstance(s, str)]
	init = next(m for m in text_sends if m.get("type") == "init")
	assert init["voice"] == "aura-2-thalia-en"
	assert init["config"]["sample_rate"] == 24000


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
