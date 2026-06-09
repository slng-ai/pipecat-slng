# pipecat-slng

_Built and maintained by the SLNG team (slng.ai)._

WebSocket STT and TTS services for [Pipecat](https://github.com/pipecat-ai/pipecat),
backed by [SLNG](https://slng.ai) — a unified voice AI gateway that routes to
multiple STT/TTS providers (Deepgram, ElevenLabs, Rime, Sarvam, and more)
through a single API key. Swap the `model` string to switch providers; no other
code changes needed.

> Tested with Pipecat v1.3.0.

## Installation

```bash
uv add pipecat-slng
# or
pip install pipecat-slng
```

## Environment variables

```env
SLNG_API_KEY=your_slng_api_key
OPENAI_API_KEY=your_openai_api_key
```

## Usage (streaming WebSocket — recommended)

`SlngSTTService` and `SlngTTSService` run over WebSocket: low-latency, supports
mid-utterance interruption, and exposes the full SLNG config surface
(encoding, sample_rate, language, speed).

```python
import os

from pipecat_slng import SlngSTTService, SlngTTSService

stt = SlngSTTService(
    api_key=os.getenv("SLNG_API_KEY"),
    model="slng/deepgram/nova:3-en",
)

tts = SlngTTSService(
    api_key=os.getenv("SLNG_API_KEY"),
    model="slng/deepgram/aura:2-en",
    voice="aura-2-thalia-en",
)
```

Common runtime knobs are top-level kwargs (e.g. `language=`, `speed=`,
`enable_vad=`, `enable_partials=`). For richer overrides pass a
`SlngSTTSettings(...)` / `SlngTTSSettings(...)` to `settings=`.

## HTTP TTS (non-streaming fallback)

For simple request/response synthesis where streaming is not required, use
`SlngHttpTTSService`. It issues one HTTP POST per utterance and returns the
full audio body in one frame.

```python
import os

from pipecat_slng import SlngHttpTTSService

tts = SlngHttpTTSService(
    api_key=os.getenv("SLNG_API_KEY"),
    model="slng/deepgram/aura:2-en",
    voice="aura-2-thalia-en",
)
```

**HTTP contract limits.** Per the SLNG Unified TTS HTTP OpenAPI, the request
body accepts **only `{text, voice}`** — there is no `config` object. Encoding,
sample_rate, language, and speed are therefore **not configurable over HTTP**;
the server returns its default audio format. The service auto-detects WAV
(decoded to raw PCM at the file's sample rate) and plain PCM (passed through
at the pipeline's sample rate). Compressed responses (MP3/Ogg) yield an
`ErrorFrame` — use the streaming `SlngTTSService` if you need codec control.

An `aiohttp.ClientSession` is created internally if you don't pass one; supply
`aiohttp_session=...` to reuse a shared session.

## Region routing

Both services support gateway region routing via `region_override` (pin to a
datacenter: `ap-southeast-2` | `eu-north-1` | `us-east-1`) and
`world_part_override` (broad zone: `ap` | `eu` | `na`). When both are set,
`region_override` wins. WebSocket services send these as the
`X-Region-Override` / `X-World-Part-Override` headers; the HTTP service uses
the `region` / `world-part` query parameters (per the bridge contract).

```python
stt = SlngSTTService(
    api_key=os.getenv("SLNG_API_KEY"),
    model="slng/deepgram/nova:3-en",
    region_override="eu-north-1",
)
```

## Example

A complete cascade bot (STT → LLM → TTS, WebSocket TTS by default) lives in
[`examples/bot.py`](examples/bot.py):

```bash
uv run --extra example examples/bot.py
```

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
