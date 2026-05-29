# pipecat-slng Community Integration — Design

Date: 2026-05-29
Status: Approved

## Goal

Convert the existing in-progress SLNG ↔ Pipecat integration from the Pipecat
core-contribution route (code living under `pipecat.services.slng`) to a
standalone **community integration** package per
[COMMUNITY_INTEGRATIONS.md](https://github.com/pipecat-ai/pipecat/blob/main/COMMUNITY_INTEGRATIONS.md).

Scope is **WebSocket only**. All HTTP variants and the `voiceai_sdk` dependency
are removed.

## Context

The repo already contains working WS service code in `pipecat_slng/stt.py` and
`pipecat_slng/tts.py`, plus HTTP variants we no longer want. `__init__.py`
currently re-imports from `pipecat.services.slng.*` (the core route), which is
wrong for a community package.

Two real community integrations confirm the target layout:
- https://github.com/usemoss/pipecat-moss (`pipecat-moss` / `pipecat_moss`)
- https://github.com/Anannas-AI/anannas-pipecat-integration (`pipecat-anannas` / `pipecat_anannas`)

Both use: PyPI name `pipecat-{vendor}`, a **top-level** import module
`pipecat_{vendor}` (NOT a `pipecat` PEP 420 namespace), a `src/` layout,
`examples/`, BSD-2 `LICENSE`, `CHANGELOG.md`, `README.md`, `pyproject.toml`,
`uv.lock`, and `.github/workflows/`.

PyPI publishing is **not required now**; the package is structured to be
pip-installable so publishing is a later, optional step.

## Naming

- PyPI distribution: `pipecat-slng`
- Import module: `pipecat_slng` (top-level)
- Usage: `from pipecat_slng import SlngSTTService, SlngTTSService`

## Target layout

```
slng_pipecat_integration/
├── src/pipecat_slng/
│   ├── __init__.py     # exports SlngSTTService, SlngSTTSettings, SlngTTSService, SlngTTSSettings
│   ├── stt.py          # SlngSTTService + SlngSTTSettings  (WS only)
│   └── tts.py          # SlngTTSService + SlngTTSSettings  (WS only)
├── examples/
│   └── bot.py          # single foundational example (current README bot)
├── tests/
│   ├── conftest.py     # fake-WS fixtures
│   ├── test_stt.py
│   ├── test_tts.py
│   └── test_live_smoke.py  # gated on SLNG_API_KEY
├── .env.example
├── README.md
├── CHANGELOG.md
├── LICENSE             # BSD-2-Clause
├── pyproject.toml
└── uv.lock
```

Move `pipecat_slng/` → `src/pipecat_slng/`. Delete `main.py` (scaffold stub).
Fold the package-level `README.md` content into the top-level `README.md`.

## Component changes

### `src/pipecat_slng/stt.py`
- Keep `SlngSTTSettings` and `SlngSTTService` (WebSocket) unchanged in behaviour.
- Delete `SlngHttpSTTService` and the `voiceai_sdk` import block.
- Update install hint string `pip install pipecat-ai[slng]` → `pip install pipecat-slng`.

### `src/pipecat_slng/tts.py`
- Keep `SlngTTSSettings` and `SlngTTSService` (WebSocket) unchanged in behaviour.
- Delete `SlngHttpTTSService` and the `voiceai_sdk` import block.
- Same install-hint fix.

### `src/pipecat_slng/__init__.py`
- Import from local `.stt` / `.tts` (NOT `pipecat.services.slng`).
- Export: `SlngSTTService`, `SlngSTTSettings`, `SlngTTSService`, `SlngTTSSettings`.
- Remove all `*Http*` exports.

### Attribution
- Update copyright header from `Daily` to `slng.ai` for community attribution.

## pyproject.toml

- `name = "pipecat-slng"`, `version = "0.1.0"`, `requires-python = ">=3.11"`.
- `build-system` = hatchling.
- `[tool.hatch.build.targets.wheel] packages = ["src/pipecat_slng"]`.
- Runtime dependencies: `pipecat-ai>=1.3.0`, `websockets`. (`loguru` arrives via `pipecat-ai`.)
- `[project.optional-dependencies] example = ["pipecat-ai[runner,silero,webrtc,openai]>=1.3.0"]`.
- dev group: `ruff`, `pytest`, `pytest-asyncio`.
- Keep existing ruff config.

## Data flow (existing WS logic — preserved)

### STT
1. Connect `wss://api.slng.ai/v1/bridges/unmute/stt/{model}` with `Authorization: Bearer` header (+ optional region/world-part headers).
2. Send `{"type":"init","config":{...}}`; wait for server `ready` (gated by `_ready_event`, 5s timeout).
3. Stream audio as raw binary WS frames.
4. Server emits `partial_transcript` → `InterimTranscriptionFrame`, `final_transcript` → `TranscriptionFrame`.
5. `finalize` sent on `VADUserStoppedSpeakingFrame`; `keepalive` periodic; `close` on disconnect.

### TTS
1. Connect `wss://api.slng.ai/v1/bridges/unmute/tts/{model}`; send `init` (voice top-level), wait for `ready`.
2. Send `{"type":"text","text":...}` to synthesize; `{"type":"flush"}` ends utterance; `{"type":"clear"}` on interruption.
3. Server returns binary audio → `TTSAudioRawFrame`; `flushed` → `TTSStoppedFrame`; `close` on disconnect.

## Error handling
Preserve existing `push_error` patterns and metric stop calls on error/disconnect.

## Tests (Unit mock + live smoke)

- pytest + pytest-asyncio.
- Patch `websocket_connect` with an async stub that accepts client sends and
  yields scripted server frames (JSON text + binary), letting tests drive the
  receive loop deterministically.
- Unit coverage:
  - STT: init message sent with correct config; `ready` gate; binary audio send;
    `partial_transcript`→interim frame, `final_transcript`→transcription frame;
    `finalize` on VAD stop; error payload → `push_error`; keepalive.
  - TTS: init (voice top-level); `ready` gate; `text` send; binary audio →
    `TTSAudioRawFrame`; `flushed` → `TTSStoppedFrame`; `clear` on interruption;
    error path.
- `test_live_smoke.py`: `@pytest.mark.skipif(not os.getenv("SLNG_API_KEY"))`,
  real `wss://api.slng.ai` round-trip for STT and TTS.

## Verification

- `ruff check` + `ruff format --check`.
- `uv run pytest` (unit; live smoke runs only with `SLNG_API_KEY`).
- Import smoke: `python -c "from pipecat_slng import SlngSTTService, SlngTTSService"`.
- Example dry-run / import check on `examples/bot.py`.

## Risks / to verify during implementation

- Confirm pipecat-ai>=1.3.0 exposes `WebsocketSTTService`, `WebsocketTTSService`,
  and `pipecat.services.settings` symbols (`NOT_GIVEN`, `STTSettings`,
  `TTSSettings`, `_NotGiven`, `is_given`) that the existing code imports.
- Confirm hatchling src-layout discovery matches the chosen package path.
