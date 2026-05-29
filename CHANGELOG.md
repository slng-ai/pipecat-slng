# Changelog

All notable changes to `pipecat-slng` are documented here. This project adheres
to [Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-05-29

### Added
- `SlngSTTService` — real-time WebSocket speech-to-text via the SLNG Unmute STT bridge.
- `SlngTTSService` — real-time WebSocket text-to-speech via the SLNG Unmute TTS bridge.
- Region routing via `region_override` / `world_part_override`.
- Foundational cascade example (`examples/bot.py`).
- Unit tests (fake WebSocket) and gated live smoke tests.

Tested with Pipecat v1.3.0.
