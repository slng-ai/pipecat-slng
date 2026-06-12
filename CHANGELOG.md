# Changelog

All notable changes to `pipecat-slng` are documented here. This project adheres
to [Semantic Versioning](https://semver.org/).

## [0.4.0] - 2026-06-12

### Added
- BYOK (Bring Your Own Key): new `provider_key` constructor kwarg on
  `SlngSTTService`, `SlngTTSService`, and `SlngHttpTTSService`. When set, the
  key is sent as the `X-Slng-Provider-Key` header on the WebSocket upgrade /
  HTTP request, so the upstream provider bills your account directly and no
  SLNG audio-minute fees apply. Only valid on external catalog routes (model
  strings without the `slng/` prefix, e.g. `deepgram/aura:2`,
  `deepgram/nova:3`); `slng/...` routes reject the header with a 400. Defaults
  to `None` — no wire change for existing call sites. See
  [BYOK docs](https://docs.slng.ai/execution-layer/byok).
- Unit tests asserting the BYOK header is present when `provider_key` is set
  and absent when it is not, for all three services.
- README "Bring your own key (BYOK)" section with external-route requirement
  and error surfaces.

### Changed
- WebSocket connect-rejection errors now include the server's response body,
  not just the HTTP status — e.g. a BYOK request to an `slng/...` route now
  reports *"HTTP 400 — BYOK is only supported for external STT/TTS routes"*
  instead of a bare `HTTP 400`.

## [0.3.0] - 2026-06-10

### Fixed
- `SlngTTSService` now treats a server-initiated WebSocket close after `audio_end`/`flushed` as the expected per-utterance lifecycle (observed with `slng/rime/arcana` models) and reconnects quietly. Previously every bot turn triggered Pipecat reconnect warnings, and three short turns in a row could trip Pipecat's consecutive quick-failure cap and shut the TTS service down mid-call. Unexpected closes keep the full Pipecat retry/failure machinery.

### Added
- Top-level constructor kwargs for runtime-tunable settings:
  - `SlngSTTService`: `language`, `enable_vad`, `enable_partials`
  - `SlngTTSService`: `language`, `speed`
  - `SlngHttpTTSService`: `language`, `speed` (kept for parity; not sent over wire — HTTP body is `{text, voice}` only per the SLNG OpenAPI)
- STT confidence filter: drop transcripts with top-level `confidence < 0.5`, matching the Pipecat community-integration guide. No-op when the bridge does not surface confidence.
- `py.typed` marker (PEP 561) — downstream type checkers now see inline types.
- GitHub Actions CI workflow: ruff + ruff-format + ty + pytest matrix on Python 3.11/3.12/3.13.
- New unit tests covering region/world routing headers, WS-TTS interruption (`clear`/`flush`), STT finalize (`finalize` + `from_finalize`→`confirm_finalize`), and graceful disconnect (`{type: close}`). Suite now 23 unit + 3 live (gated).

### Changed
- Error handling tightened to the community-integration guide ("raise AND push"):
  - `_connect_websocket` (STT + WS-TTS) now raises after `push_error`, so connect failures surface through `PipelineRunner` instead of dribbling silent send-after-disconnect errors.
  - In-stream send / non-200 / compressed-format paths in `run_stt` and `run_tts` (WS + HTTP) now call `push_error` alongside the existing `yield ErrorFrame`.
- `examples/bot.py` defaults to the streaming `SlngTTSService`; removed the three commented-out TTS variants (incl. the "Problematic provider" Cartesia stub).
- `README.md` reorganised "WebSocket first, HTTP fallback"; added explicit company attribution under the title; documented the HTTP body contract (`{text, voice}` only).

Tested with Pipecat v1.3.0.

## [0.2.0] - 2026-05-29

### Added
- `SlngHttpTTSService` — non-streaming HTTP/REST text-to-speech via the SLNG Unified TTS bridge (`POST /v1/bridges/unmute/tts/{model}`), built on `aiohttp`.

### Changed
- `SlngTTSService` now applies runtime settings updates: a `voice`/`speed`/`language` change reconnects to re-run the init handshake.

Tested with Pipecat v1.3.0.

## [0.1.0] - 2026-05-29

### Added
- `SlngSTTService` — real-time WebSocket speech-to-text via the SLNG Unmute STT bridge.
- `SlngTTSService` — real-time WebSocket text-to-speech via the SLNG Unmute TTS bridge.
- Region routing via `region_override` / `world_part_override`.
- Foundational cascade example (`examples/bot.py`).
- Unit tests (fake WebSocket) and gated live smoke tests.

Tested with Pipecat v1.3.0.
