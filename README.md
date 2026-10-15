# pipecat-slng

[![CI](https://github.com/slng-ai/pipecat-slng/actions/workflows/ci.yml/badge.svg)](https://github.com/slng-ai/pipecat-slng/actions/workflows/ci.yml)

_Built and maintained by the SLNG team (slng.ai)._

WebSocket STT and TTS services for [Pipecat](https://github.com/pipecat-ai/pipecat),
backed by [SLNG](https://slng.ai) — a unified voice AI gateway that routes to
multiple STT/TTS providers (Deepgram, ElevenLabs, Rime, Sarvam, and more)
through a single API key. Swap the `model` string to switch providers; no other
code changes needed.

> Requires Pipecat v1.8.0 or newer, and is tested against v1.8.0. Earlier
> `pipecat-slng` releases do not import on Pipecat 1.8.0.

## Installation

```bash
uv add pipecat-slng
# or
pip install pipecat-slng
```

## Environment variables

```env
SLNG_API_KEY=your_slng_api_key      # get one at https://slng.ai
SLNG_WORLD_PART=us-east             # SLNG region, see "Regions" below
OPENAI_API_KEY=your_openai_api_key  # only needed for the example bot (LLM)
```

Copy [`.env.example`](.env.example) to `.env` to get started.

## Usage (streaming WebSocket — recommended)

`SlngSTTService` and `SlngTTSService` run over WebSocket: low-latency, supports
mid-utterance interruption, and exposes the full SLNG config surface
(encoding, sample_rate, language, speed).

```python
import os
import uuid

from pipecat_slng import SlngSTTService, SlngTTSService

agent_id = "my-agent"           # names your agent in SLNG usage
session_id = str(uuid.uuid4())  # one id per call; use your own call id if you have one

stt = SlngSTTService(
    api_key=os.getenv("SLNG_API_KEY"),
    world_part="us-east",
    model="slng/deepgram/nova:3-en",
    external_agent_id=agent_id,
    external_session_id=session_id,
)

tts = SlngTTSService(
    api_key=os.getenv("SLNG_API_KEY"),
    world_part="us-east",
    model="slng/deepgram/aura:2-en",
    voice="aura-2-thalia-en",
    external_agent_id=agent_id,
    external_session_id=session_id,
)
```

Always pass `external_agent_id` and `external_session_id`. SLNG uses them to
group your usage by agent and by call. See
[Usage tracking](#usage-tracking-agent-id-and-session-id).

Common runtime knobs are top-level kwargs (e.g. `language=`, `speed=`,
`enable_partials=`). For richer overrides pass a
`SlngSTTSettings(...)` / `SlngTTSSettings(...)` to `settings=`.

Defaults when not specified: STT sends no `language`, and uses
`enable_partials=True`; TTS uses `language=Language.EN` and the server's
default `speed`.

When `language` is omitted, the gateway applies the selected model's default
language, which the SLNG catalog defines per model: for example `hi` on
`slng/deepgram/nova:3-hi`, `en` on Soniox, and `hi-IN` on Amazon Transcribe.
On language-specific routes such as `nova:3-hi` the route already fixes the
language, so pass `language=` only to override a model's default.

`enable_partials` is honoured only by models that declare it (Deepgram Nova,
Soniox, Speechmatics, Reson8). Other models, such as Sarvam Saaras, return
final transcripts only and ignore it.

Three behaviors worth knowing:

- **Confidence filter (STT).** When the provider surfaces a confidence score,
  *partial* transcripts below 0.5 are dropped. Finals are never dropped —
  discarding one hangs the user turn rather than losing a word.
- **Turn finalization (STT).** On `VADUserStoppedSpeakingFrame` the service
  sends `finalize` and marks the answering transcript
  `TranscriptionFrame.finalized`, which is what lets Pipecat end the user turn
  immediately instead of waiting out its safety-net timer. That timer is sized
  by `ttfs_p99_latency`, for which this service declares no default, so Pipecat
  substitutes a conservative 1.0 s and logs a warning at pipeline start. It only
  affects the fallback path — a finalized transcript cancels the timer before it
  fires — so the warning is cosmetic.

  To right-size the fallback, pass your own value:
  `SlngSTTService(..., ttfs_p99_latency=<seconds>)`. Measure it with
  [stt-benchmark](https://github.com/pipecat-ai/stt-benchmark) at
  `VADParams.stop_secs=0.2`, the threshold Pipecat's built-in values assume.
  Indicative figures from our own harness (speech-end → final transcript, small
  sample, one network path — **not** a substitute for a benchmarked P99):
  ~605 ms median on `slng/deepgram/nova:3-en` and ~280 ms on `deepgram/nova:3`.
  Prefer a value above your observed maximum: too low expires the safety net
  early and cuts the caller off, which is worse than waiting.
- **Runtime settings updates.** Changing `voice`, `speed`, or `language`
  mid-session (via Pipecat settings updates) reconnects the WebSocket to
  re-run the init handshake — expect a brief reconnect, not a silent no-op.

### Usage tracking: agent id and session id

Pass `external_agent_id` and `external_session_id` to every service you build.
SLNG uses them to group usage by agent and by call.

- `external_agent_id`: a stable name for your agent, e.g. `"support-bot"`.
- `external_session_id`: one id per call. Use the id your telephony or
  transport already gives you, e.g. a Twilio `CallSid` or Pipecat's
  `runner_args.session_id`.
- If you leave them out, the service still works, but SLNG cannot tell your
  agents and calls apart.
- They are sent as the `X-SLNG-Agent-Id` and `X-SLNG-Session-Id` headers on
  every WebSocket the service opens. For TTS this includes reconnects and the
  warm standby socket.
- Each value is stripped, must be 1 to 128 characters, and must not contain a
  comma or a control character. A bad value raises `ValueError` when the
  service is built.
- The names and rules are the same as in `livekit-plugins-slng`.

The ids are fixed for the life of the service. Build new services for each
call. The [full example](#full-example-with-usage-tracking) below does this.

### Optional TTS warm standby

Enable one prepared connection for the next utterance:

```python
tts = SlngTTSService(
    api_key=os.environ["SLNG_API_KEY"],
    world_part="eu-west",  # Gradium runs in the EU regions
    model="gradium/tts:default",
    voice="QETTJoT4n_WmpL3w",
    warm_standby_enabled=True,  # default: False
)
```

Preparation starts after the first successful text send. The next utterance
uses the spare only after that connection acknowledges `ready`; a pending,
expired, or failed spare falls back to ordinary connection handling. Startup
still uses the initial connection. All fragments of an utterance stay together;
Pipecat's default `reuse_context_id_within_turn=True` is required.

When enabled, the service rotates to a ready spare even if the old connection
remains open. It waits for preceding synthesis to finish, preserves queued
playback, and closes the old transport in the background. At most two
connections are opening, open, or closing. Settings changes invalidate prepared
connections; interruption retires unfinished synthesis; shutdown closes both.
Preparation uses an extra gateway/provider session, which can consume connection
quota or incur provider charges even when unused.

Both initialized connections receive the existing 30-second keepalive. This
cannot prevent every provider expiry, and gateway `ready` does not guarantee
that all provider preparation has finished. An expired spare is retried after
later speech, without synthetic warmup text or a continuous reconnect loop.
Debug logs report actual use, miss reason, and background preparation time by
synthesis context, without logging speech or credentials in those records.

In September 2026 tests with Pipecat 1.8.0, Gradium's controlled per-utterance
reconnection case improved median request-to-first-audio from **1,079–1,415 ms**
across baseline batches to **77–109 ms** with standby. Each condition used five
utterances, interleaving baseline/standby/baseline twice with 1.5-second gaps;
these medians exclude startup. Reconnection was forced by the test to isolate
its cost. Normal Gradium, Deepgram, and Sarvam reuse showed no consistent improvement.
These client measurements do not identify deployed gateway revision or provider
cache state; measure your route before enabling the option.

The opt-in measurement test supports `SLNG_TTS_STANDBY_COMPARE=1`, alongside
`SLNG_API_KEY`, `SLNG_TTS_MEASURE=1`, and explicit `SLNG_TTS_MODEL` and
`SLNG_TTS_VOICE`. `SLNG_TTS_FORCE_RECONNECT=1` selects the labelled controlled
condition. These are test inputs, not service configuration.

### Pronunciation dictionaries

Streaming TTS can use one SLNG pronunciation dictionary as the WebSocket
session default. Reference it by name or immutable ID; SLNG validates and
applies the rewrite rules.

```python
tts = SlngTTSService(
    api_key=os.getenv("SLNG_API_KEY"),
    world_part="us-east",
    model="slng/deepgram/aura:2-en",
    voice="aura-2-thalia-en",
    pronunciation={"mode": "rewrite", "name": "support-pronunciations"},
)
```

Use `{"mode": "rewrite", "dictionary_id": "pd_..."}` to reference an
immutable dictionary version. See the [pronunciation dictionary docs](https://docs.slng.ai/pronunciation-dictionaries).

## Regions

Every service needs a `world_part`. It picks the SLNG region that handles the
request. Each region has its own gateway at `{world_part}.api.slng.ai`.
Requests are not routed between regions.

For example, Soniox STT and Fish TTS, both in Germany (`eu-west`):

```python
import os
import uuid

from pipecat.transcriptions.language import Language
from pipecat_slng import SlngSTTService, SlngTTSService

session_id = str(uuid.uuid4())

stt = SlngSTTService(
    api_key=os.getenv("SLNG_API_KEY"),
    world_part="eu-west",  # or WorldPart.EU_WEST
    model="soniox/speech-ai:rt-v5",
    language=Language.EN,
    external_agent_id="my-agent",
    external_session_id=session_id,
)

tts = SlngTTSService(
    api_key=os.getenv("SLNG_API_KEY"),
    world_part="eu-west",
    model="slng/fish/tts:s2.1-pro",
    voice="1258154bb41540c7b9dc0d10cd6704bf",
    language=Language.EN,
    external_agent_id="my-agent",
    external_session_id=session_id,
)
```

Each service takes its own `world_part`, so STT and TTS can run in different
regions.

| Region | `world_part` |
| - | - |
| Netherlands | `eu-north` |
| Germany | `eu-west` |
| United States (East) | `us-east` |
| United States (West) | `us-west` |
| Australia | `au` |
| Brazil | `br` |
| United Kingdom | `gb` |
| Indonesia | `id` |
| Israel | `il` |
| India | `in` |
| Japan | `jp` |
| Singapore | `sg` |
| South Africa | `za` |

Models differ by region. Check
[models by region](https://docs.slng.ai/models/catalog/by-region) before you
pick one. The default `slng/deepgram/...` models run in `us-east`.

### Moving from `base_url`

`base_url` is deprecated. It still works if its host matches `world_part`, and
it logs a `DeprecationWarning`:

```python
# Works, with a warning. Remove base_url.
SlngSTTService(api_key=key, world_part="in", base_url="in.api.slng.ai")

# ValueError: the global api.slng.ai gateway is no longer supported.
SlngSTTService(api_key=key, world_part="in", base_url="api.slng.ai")

# ValueError: the host does not match world_part.
SlngSTTService(api_key=key, world_part="us-west", base_url="in.api.slng.ai")
```

`region_override` and `world_part_override` are removed. Use `world_part`.

## Model routing & bring-your-own-key (BYOK)

The `model` string decides where transcription/synthesis runs:

- **`slng/...`** (e.g. `slng/deepgram/aura:2-en`) — hosted by SLNG.
- **anything else** (e.g. `deepgram/aura:2`, `elevenlabs/...`, `cartesia/sonic:3`,
  `sarvam/bulbul:v3`) — an **external** provider, proxied through SLNG.

An external route works **with or without** your own provider key — those are
two independent choices. The `slng/` prefix is what selects SLNG-hosted; BYOK is
a separate decision layered on top. The full matrix:

| `model` | `provider_key` | Runs on | Billed by |
|---|---|---|---|
| `slng/deepgram/aura:2-en` | — | SLNG (self-hosted) | SLNG (audio-minutes) |
| `deepgram/aura:2` | — | SLNG's own provider account | SLNG (audio-minutes) |
| `deepgram/aura:2` | your key | **your** provider account | the provider (BYOK) |
| `slng/...` | your key | — | **rejected — HTTP 400** |

For BYOK, pass your own provider key via `provider_key`. It is forwarded as the
`X-Slng-Provider-Key` header, so the provider bills your account directly and no
SLNG audio-minute fees apply — the SLNG cache still applies on top. This is a
**separate** key from `SLNG_API_KEY`, which always authenticates you to SLNG.
See the [BYOK docs](https://docs.slng.ai/execution-layer/byok).

```python
# BYOK = an external route + your own provider key. Deepgram is shown here; the
# same pattern works for any external provider (ElevenLabs, Cartesia, Sarvam, …).
stt = SlngSTTService(
    api_key=os.getenv("SLNG_API_KEY"),            # authenticates you to SLNG
    world_part="us-east",
    model="deepgram/nova:3",                      # external route — no slng/ prefix
    provider_key=os.getenv("SLNG_PROVIDER_KEY"),  # your own provider key
    external_agent_id="my-agent",
    external_session_id=session_id,
)

tts = SlngTTSService(
    api_key=os.getenv("SLNG_API_KEY"),
    world_part="us-east",
    model="deepgram/aura:2",                      # external route — no slng/ prefix
    voice="aura-2-thalia-en",
    provider_key=os.getenv("SLNG_PROVIDER_KEY"),
    external_agent_id="my-agent",
    external_session_id=session_id,
)
```

BYOK is valid only on **external** routes; an `slng/...` route plus a
`provider_key` is rejected with a 400 (*"BYOK is only supported for external
STT/TTS routes"*). If the provider rejects your key, the failure surfaces as a
`backend_connection_failed` error frame with the upstream 401/403 detail.

## Example

A complete cascade bot (STT → LLM → TTS, WebSocket TTS by default) lives in
[`examples/bot.py`](examples/bot.py):

```bash
cp .env.example .env   # fill in SLNG_API_KEY and OPENAI_API_KEY
uv run --extra example examples/bot.py
```

Then open http://localhost:7860/client in your browser and start talking.
The bot sends `SLNG_AGENT_ID` (default `pipecat-slng-example`) and the runner's
session id on every connection.
Pick the region with `SLNG_WORLD_PART` (default `us-east`).
Pick models with `SLNG_STT_MODEL` / `SLNG_TTS_MODEL` (both default to `slng/...`
self-hosted routes); set `SLNG_PROVIDER_KEY` to your own provider key to run an
external route in BYOK mode. The bot uses the SmallWebRTC transport by default;
pass `-t daily` to use Daily instead (requires installing `pipecat-ai[daily]`).

### Full example with usage tracking

A complete bot with a weather tool. It sends the agent id and the session id on
every SLNG connection:

- `agent_id` comes from `SLNG_AGENT_ID`.
- `session_id` is the id Pipecat's runner gives each session. The bot makes a
  new UUID if the runner gives none.

It needs `pipecat-slng>=0.7.0`, `pipecat-ai[daily,openai,runner,silero,webrtc]`
and `dotenv`. Run it with `uv run bot.py`.

<details>
<summary><code>bot.py</code></summary>

```python
"""Pipecat voice bot: SLNG STT -> OpenAI LLM -> SLNG TTS.

Transports: only ``webrtc`` is wired, for browser testing. Production swaps in
``twilio`` (``FastAPIWebsocketParams``, which sets ``add_wav_header`` and the
serializer itself) or ``daily`` (``DailyParams``); both take the same
``audio_in_enabled``/``audio_out_enabled`` pair.

Environment:
    SLNG_API_KEY, OPENAI_API_KEY -- required.
    SLNG_STT_MODEL -- an ``slng/...`` route is SLNG-hosted; anything else is an
        external provider proxied through SLNG.
    SLNG_STT_TTFS_P99 -- the safety-net timer Pipecat waits out when STT never
        finalizes a transcript. The plugin finalizes as of 0.5.0, so this now
        only covers an unanswered finalize, but leaving it unset means a 1.0s
        default plus a warning on every run. Measured at the default
        ``stop_secs=0.2``: 605ms median on ``slng/deepgram/nova:3-en``, 280ms on
        ``deepgram/nova:3``. The 0.7 default clears both.
    SLNG_TTS_MODEL, SLNG_TTS_VOICE -- TTS route and voice.

Each service needs ``world_part``, the SLNG region to call, e.g. ``"in"`` or
``"us-west"``. Models differ by region:
https://docs.slng.ai/models/catalog/by-region
"""

import logging
import os
import uuid
from collections.abc import Callable

from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import TTSSpeakFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
	LLMContextAggregatorPair,
	LLMUserAggregatorParams,
)
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.transcriptions.language import Language
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.workers.runner import WorkerRunner
from pipecat_slng import SlngSTTService, SlngTTSService
from dotenv import load_dotenv
from tools.weather import WEATHER_SCHEMA, get_current_weather

load_dotenv()


logger = logging.getLogger(__name__)


transport_params: dict[str, Callable[..., TransportParams]] = {
	"webrtc": lambda: TransportParams(
		audio_in_enabled=True,
		audio_out_enabled=True,
	),
}


async def run_bot(transport: BaseTransport, runner_args: RunnerArguments):
	"""Build the STT/LLM/TTS pipeline and run it until the client disconnects."""
	logging.basicConfig(filename="myapp.log", level=logging.INFO)
	logger.info("Starting bot")
	slng_api_key = os.environ["SLNG_API_KEY"]
	# SLNG groups usage by these ids (X-SLNG-Agent-Id / X-SLNG-Session-Id headers).
	agent_id = os.getenv("SLNG_AGENT_ID", "pipecat-slng-example")
	session_id = runner_args.session_id or str(uuid.uuid4())
	logger.info(f"SLNG tracking: agent_id={agent_id} session_id={session_id}")

	runner = WorkerRunner(handle_sigint=runner_args.handle_sigint)

	stt = SlngSTTService(
		api_key=slng_api_key,
		model="soniox/speech-ai:rt-v5",
		language=Language.EN,
		world_part="eu-north",
		external_agent_id=agent_id,
		external_session_id=session_id,
	)

	llm = OpenAILLMService(
		api_key=os.environ["OPENAI_API_KEY"],
		settings=OpenAILLMService.Settings(
			system_instruction="You are a helpful voice assistant. Keep responses brief.",
		),
	)

	tts = SlngTTSService(
		api_key=slng_api_key,
		model="slng/fish/tts:s2.1-pro",
		voice="1258154bb41540c7b9dc0d10cd6704bf",
		language=Language.EN,
		world_part="eu-north",
		warm_standby_enabled=True,
		external_agent_id=agent_id,
		external_session_id=session_id,
	)

	llm.register_function("get_current_weather", get_current_weather)

	context = LLMContext(tools=ToolsSchema(standard_tools=[WEATHER_SCHEMA]))
	aggregators = LLMContextAggregatorPair(
		context,
		user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer()),
	)

	pipeline = Pipeline(
		[
			transport.input(),
			stt,
			aggregators.user(),
			llm,
			tts,
			transport.output(),
			aggregators.assistant(),
		]
	)

	agent_1 = PipelineWorker(
		pipeline,
		name="assistant",
		params=PipelineParams(
			audio_in_sample_rate=16000,
			audio_out_sample_rate=24000,
			enable_metrics=True,
			enable_usage_metrics=True,
		),
	)

	@agent_1.rtvi.event_handler("on_client_ready")
	async def on_client_ready(rtvi):
		"""Greet the caller once the client has sent "client-ready".

		This is an RTVI event, not a transport event -- the worker auto-creates
		the RTVI processor, so the handler registers on ``agent_1.rtvi``.
		"""
		await agent_1.queue_frames(
			[
				TTSSpeakFrame(
					"Hi! I am a voice assistant here to help you or chat with you. How can I help you today?"
				)
			]
		)

	@transport.event_handler("on_client_connected")
	async def on_client_connected(transport, client):
		"""Log the connection."""
		logger.info("Client connected")

	@transport.event_handler("on_client_disconnected")
	async def on_client_disconnected(transport, client):
		"""Shut the runner down when the caller hangs up."""
		await runner.cancel()

	await runner.add_workers(agent_1)
	await runner.run()


async def bot(runner_args: RunnerArguments):
	"""Runner entry point: build the transport, then run the pipeline."""
	transport = await create_transport(runner_args, transport_params)
	await run_bot(transport, runner_args)


if __name__ == "__main__":
	from pipecat.runner.run import main

	main()
```

</details>

<details>
<summary><code>tools/weather.py</code></summary>

```python
"""Weather tool for the example bot.

The schema is declared explicitly because on pipecat 1.3.0 the ``tools``
argument only accepts a ``ToolsSchema``, and "direct" functions (params plus
required named args) cannot satisfy the ``DirectFunction`` protocol under
``ty``. So the schema is advertised here and the handler is registered
separately via ``llm.register_function``.
"""

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.services.llm_service import FunctionCallParams

WEATHER_SCHEMA = FunctionSchema(
	name="get_current_weather",
	description="Get the current weather.",
	properties={
		"location": {
			"type": "string",
			"description": 'The city and state, e.g. "San Francisco, CA".',
		},
		"format": {
			"type": "string",
			"enum": ["celsius", "fahrenheit"],
			"description": "The temperature unit to use.",
		},
	},
	required=["location", "format"],
)


async def get_current_weather(params: FunctionCallParams):
	"""Handle a get_current_weather call.

	The LLM arguments arrive in ``params.arguments`` as ``location`` and
	``format``; this stub ignores them and returns a fixed reading.
	"""
	await params.result_callback({"conditions": "sunny", "temperature": "31"})
```

</details>

## Development

```bash
uv sync --all-extras
uv run pytest          # unit tests (live smoke tests skip without SLNG_API_KEY)
uv run ruff check .
uv run ty check .
```

## About SLNG

SLNG (https://slng.ai) is a unified voice AI gateway. Learn more in the
[SLNG docs](https://docs.slng.ai/).

## License

BSD-2-Clause — see [LICENSE](LICENSE).
