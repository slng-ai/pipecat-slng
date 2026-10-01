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

from pipecat_slng import SlngSTTService, SlngTTSService

stt = SlngSTTService(
    api_key=os.getenv("SLNG_API_KEY"),
    world_part="us-east",
    model="slng/deepgram/nova:3-en",
)

tts = SlngTTSService(
    api_key=os.getenv("SLNG_API_KEY"),
    world_part="us-east",
    model="slng/deepgram/aura:2-en",
    voice="aura-2-thalia-en",
)
```

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

### Optional TTS warm standby

Enable one prepared connection for the next utterance:

```python
tts = SlngTTSService(
    api_key=os.environ["SLNG_API_KEY"],
    world_part="us-east",
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

```python
tts = SlngTTSService(
    api_key=os.getenv("SLNG_API_KEY"),
    world_part="in",  # or WorldPart.IN
    model="sarvam/bulbul:v3",
    voice="shubh",
)
```

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
)

tts = SlngTTSService(
    api_key=os.getenv("SLNG_API_KEY"),
    world_part="us-east",
    model="deepgram/aura:2",                      # external route — no slng/ prefix
    voice="aura-2-thalia-en",
    provider_key=os.getenv("SLNG_PROVIDER_KEY"),
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
Pick the region with `SLNG_WORLD_PART` (default `us-east`).
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
