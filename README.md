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
OPENAI_API_KEY=your_openai_api_key  # only needed for the example bot (LLM)
```

Copy [`.env.example`](.env.example) to `.env` to get started.

The destination (`world_part`) is **not** an environment variable — it is a
required constructor parameter you define in code. See
[Destination routing](#destination-routing-world_part).

## Usage (streaming WebSocket — recommended)

`SlngSTTService` and `SlngTTSService` run over WebSocket: low-latency, supports
mid-utterance interruption, and exposes the full SLNG config surface
(encoding, sample_rate, language, speed).

```python
import os

from pipecat_slng import SlngSTTService, SlngTTSService

world_part = "eu-west"  # Germany; choose where your models are available.

stt = SlngSTTService(
    api_key=os.getenv("SLNG_API_KEY"),
    world_part=world_part,
    model="slng/deepgram/nova:3-en",
)

tts = SlngTTSService(
    api_key=os.getenv("SLNG_API_KEY"),
    world_part=world_part,
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

### Pronunciation dictionaries

Streaming TTS can use one SLNG pronunciation dictionary as the WebSocket
session default. Reference it by name or immutable ID; SLNG validates and
applies the rewrite rules.

```python
tts = SlngTTSService(
    api_key=os.getenv("SLNG_API_KEY"),
    world_part="eu-west",
    model="slng/deepgram/aura:2-en",
    voice="aura-2-thalia-en",
    pronunciation={"mode": "rewrite", "name": "support-pronunciations"},
)
```

Use `{"mode": "rewrite", "dictionary_id": "pd_..."}` to reference an
immutable dictionary version. See the [pronunciation dictionary docs](https://docs.slng.ai/pronunciation-dictionaries).

## HTTP TTS (non-streaming fallback)

For simple request/response synthesis where streaming is not required, use
`SlngHttpTTSService`. It issues one HTTP POST per utterance and returns the
full audio body in one frame.

```python
import os

from pipecat_slng import SlngHttpTTSService

tts = SlngHttpTTSService(
    api_key=os.getenv("SLNG_API_KEY"),
    world_part="eu-west",
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

## Destination routing (`world_part`)

Every service requires `world_part`, the hostname prefix of the SLNG
destination your requests go to. It has no default and no environment fallback:

| Service | Endpoint |
|---|---|
| `SlngSTTService` | `wss://{world_part}.api.slng.ai/v1/bridges/unmute/stt/{model}` |
| `SlngTTSService` | `wss://{world_part}.api.slng.ai/v1/bridges/unmute/tts/{model}` |
| `SlngHttpTTSService` | `https://{world_part}.api.slng.ai/v1/bridges/unmute/tts/{model}` |

Define the value once in your application code and pass it to both services —
`world_part` is a hostname prefix, never a full hostname, URL, or group name:

```python
world_part = "eu-west"  # Germany

stt = SlngSTTService(api_key=slng_api_key, world_part=world_part)
tts = SlngTTSService(api_key=slng_api_key, world_part=world_part)
```

### Destinations

`Group` and `Location` are labels for humans; only the world part selects a host.

| World part | Group | Location | Hostname |
|---|---|---|---|
| `us-east` | Americas | US East | `us-east.api.slng.ai` |
| `us-west` | Americas | US West | `us-west.api.slng.ai` |
| `br` | Americas | Brazil | `br.api.slng.ai` |
| `eu-west` | Europes | Germany | `eu-west.api.slng.ai` |
| `eu-north` | Europes | Finland | `eu-north.api.slng.ai` |
| `gb` | Europes | UK | `gb.api.slng.ai` |
| `za` | Europes | South Africa | `za.api.slng.ai` |
| `il` | Europes | Israel | `il.api.slng.ai` |
| `jp` | Asia | Japan | `jp.api.slng.ai` |
| `sg` | Asia | Singapore | `sg.api.slng.ai` |
| `id` | Asia | Indonesia (Jakarta) | `id.api.slng.ai` |
| `in` | Asia | Mumbai | `in.api.slng.ai` |
| `au` | Asia | Australia | `au.api.slng.ai` |

Pick one where your model is provisioned. Any syntactically valid prefix
(1–63 lowercase letters, digits, or hyphens) is accepted so new destinations
work without a package upgrade — which also means an unprovisioned destination
fails with a normal connection error. There is **no fallback**: a failing
destination is never retried against another world part or the unprefixed
gateway.

### Advanced: an explicit `base_url`

Omit `base_url` for normal use. Supplying it overrides host selection and uses
your host **unchanged** — no world part is inserted, even if the host already
has one. `world_part` stays required and validated either way.

```python
# world_part routing (recommended):
#   wss://in.api.slng.ai/v1/bridges/unmute/stt/sarvam/saaras:v3
stt = SlngSTTService(api_key=slng_api_key, world_part="in", model="sarvam/saaras:v3")

# Legacy unprefixed gateway, staging, or a custom host:
#   wss://api.slng.ai/v1/bridges/unmute/stt/sarvam/saaras:v3
legacy = SlngSTTService(
    api_key=slng_api_key,
    world_part="in",
    base_url="api.slng.ai",
    model="sarvam/saaras:v3",
)
```

| Service | Accepted `base_url` |
|---|---|
| `SlngSTTService`, `SlngTTSService` | DNS host with optional port and path; bare hosts get `wss://`, or pass `ws://`/`wss://` explicitly |
| `SlngHttpTTSService` | Full `http://` or `https://` URL with a DNS host and optional port and path |

Credentials, query strings, fragments, IP literals, and malformed ports are
rejected at construction. An empty or malformed base is an error, never a
silent switch back to automatic routing.

### Migrating from `region_override` / `world_part_override`

Both settings are removed and now raise `TypeError` with migration guidance
(including when passed as `None`), so no call silently loses its routing.

1. Pick a world part from the table above where your models are available.
2. Pass it as `world_part=` to **every** SLNG service. Do not add it to `.env`.
3. Delete `region_override=` / `world_part_override=`, and delete any
   `base_url=` you were passing — leaving one in place deliberately bypasses
   world-part routing.

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
    world_part="eu-west",                         # destination host
    model="deepgram/nova:3",                      # external route — no slng/ prefix
    provider_key=os.getenv("SLNG_PROVIDER_KEY"),  # your own provider key
)

tts = SlngTTSService(
    api_key=os.getenv("SLNG_API_KEY"),
    world_part="eu-west",
    model="deepgram/aura:2",                      # external route — no slng/ prefix
    voice="aura-2-thalia-en",
    provider_key=os.getenv("SLNG_PROVIDER_KEY"),
)
```

BYOK is valid only on **external** routes; an `slng/...` route plus a
`provider_key` is rejected with a 400 (*"BYOK is only supported for external
STT/TTS routes"*). If the provider rejects your key, the failure surfaces as a
`backend_connection_failed` error frame over WebSocket, or the upstream 401/403
with the `X-Slng-Auth-Source: client_key` response header over HTTP.

## Example

A complete cascade bot (STT → LLM → TTS, WebSocket TTS by default) lives in
[`examples/bot.py`](examples/bot.py):

```bash
cp .env.example .env   # fill in SLNG_API_KEY and OPENAI_API_KEY
uv run --extra example examples/bot.py
```

Then open http://localhost:7860/client in your browser and start talking.
Pick models with `SLNG_STT_MODEL` / `SLNG_TTS_MODEL` (both default to `slng/...`
self-hosted routes); set `SLNG_PROVIDER_KEY` to your own provider key to run an
external route in BYOK mode. The bot uses the SmallWebRTC transport by default;
pass `-t daily` to use Daily instead (requires installing `pipecat-ai[daily]`).

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
