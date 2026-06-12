# pipecat-slng

[![CI](https://github.com/slng-ai/pipecat-slng/actions/workflows/ci.yml/badge.svg)](https://github.com/slng-ai/pipecat-slng/actions/workflows/ci.yml)

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
SLNG_API_KEY=your_slng_api_key      # get one at https://slng.ai
OPENAI_API_KEY=your_openai_api_key  # only needed for the example bot (LLM)
```

Copy [`.env.example`](.env.example) to `.env` to get started.

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

Defaults when not specified: STT uses `language=Language.EN`,
`enable_vad=True`, `enable_partials=True`; TTS uses `language=Language.EN`
and the server's default `speed`.

Two behaviors worth knowing:

- **Confidence filter (STT).** When the provider surfaces a confidence
  score, transcripts below 0.5 are dropped.
- **Runtime settings updates.** Changing `voice`, `speed`, or `language`
  mid-session (via Pipecat settings updates) reconnects the WebSocket to
  re-run the init handshake — expect a brief reconnect, not a silent no-op.

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

## Bring your own key (BYOK)

If you already have a contract with an upstream provider, pass your own
provider key via `provider_key`. It is forwarded as the
`X-Slng-Provider-Key` header, so the provider bills your account directly
and no SLNG audio-minute fees apply — while the SLNG cache still applies on
top. See the [BYOK docs](https://docs.slng.ai/execution-layer/byok).

```python
stt = SlngSTTService(
    api_key=os.getenv("SLNG_API_KEY"),
    model="deepgram/nova:3",            # external route — no slng/ prefix
    provider_key=os.getenv("DEEPGRAM_API_KEY"),
)

tts = SlngTTSService(
    api_key=os.getenv("SLNG_API_KEY"),
    model="deepgram/aura:2",            # external route — no slng/ prefix
    voice="aura-2-thalia-en",
    provider_key=os.getenv("DEEPGRAM_API_KEY"),
)
```

BYOK only works on **external** catalog routes (model strings without the
`slng/` prefix, e.g. `deepgram/aura:2`, `deepgram/nova:3`). SLNG-hosted
`slng/...` routes reject the header with a 400. If the upstream provider
rejects your key, the failure surfaces as a `backend_connection_failed`
error frame over WebSocket, or the upstream 401/403 with the
`X-Slng-Auth-Source: client_key` response header over HTTP.

## Example

A complete cascade bot (STT → LLM → TTS, WebSocket TTS by default) lives in
[`examples/bot.py`](examples/bot.py):

```bash
cp .env.example .env   # fill in SLNG_API_KEY and OPENAI_API_KEY
uv run --extra example examples/bot.py
```

Then open http://localhost:7860/client in your browser and start talking.
The bot uses the SmallWebRTC transport by default; pass `-t daily` to use
Daily instead (requires installing `pipecat-ai[daily]`).

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
