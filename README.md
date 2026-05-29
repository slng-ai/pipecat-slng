# pipecat-slng

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

## Usage

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

## Model variants

| Provider | STT model | TTS model |
|----------|-----------|-----------|
| Deepgram | `slng/deepgram/nova:3-en` | `slng/deepgram/aura:2-en` |
| ElevenLabs | `slng/elevenlabs/scribe:v1-en` | `slng/elevenlabs/multilingual:v2` |
| Rime | — | `slng/rime/arcana:v2` |
| Sarvam | `slng/sarvam/saarika:v2-hi` | `slng/sarvam/bulbul:v2-hi` |

## Region routing

Both services support gateway region routing via `region_override` (pin to a
datacenter: `ap-southeast-2` | `eu-north-1` | `us-east-1`) and
`world_part_override` (broad zone: `ap` | `eu` | `na`). When both are set,
`region_override` wins. These map to the `X-Region-Override` and
`X-World-Part-Override` headers.

```python
stt = SlngSTTService(
    api_key=os.getenv("SLNG_API_KEY"),
    model="slng/deepgram/nova:3-en",
    region_override="eu-north-1",
)
```

## Example

A complete cascade bot (STT → LLM → TTS) lives in [`examples/bot.py`](examples/bot.py):

```bash
uv run --extra example examples/bot.py
```

## Development

```bash
uv sync --all-extras
uv run pytest          # unit tests (live smoke tests skip without SLNG_API_KEY)
uv run ruff check .
```

## About SLNG

SLNG (https://slng.ai) is a unified voice AI gateway. Learn more in the
[SLNG docs](https://docs.slng.ai/).

## License

BSD 2-Clause. See [LICENSE](LICENSE).
